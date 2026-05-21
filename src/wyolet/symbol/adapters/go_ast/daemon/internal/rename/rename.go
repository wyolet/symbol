// Package rename implements project-wide rename analysis for Go using
// go/types via golang.org/x/tools/go/packages. Loads the project once,
// classifies every reference site (SelectorExpr.Sel matching the leaf,
// FuncDecl.Name matching the leaf) against the rename target using
// semantic-correct receiver resolution.
//
// Unlike a tier-1 AST-only resolver, this handles interface dispatch,
// embedded methods, generics, and factory-returned receivers correctly
// via info.Selections[sel].Recv().
package rename

import (
	"fmt"
	"go/ast"
	"go/token"
	"go/types"
	"path/filepath"

	"github.com/wyolet/symbol/go-scan/internal/rpc"
	"golang.org/x/tools/go/packages"
)

// Member runs project-wide rename analysis for a method (or other
// receiver-attached member).
func Member(params rpc.RenameMemberParams) (rpc.RenameResult, error) {
	pkgs, err := loadPackages(params.ProjectRoot)
	if err != nil {
		return rpc.RenameResult{}, err
	}

	candidateSet := stringSet(params.CandidateFiles)
	result := rpc.RenameResult{Files: map[string]rpc.RenameFileAnalysis{}}

	for _, pkg := range pkgs {
		if pkg.TypesInfo == nil {
			continue
		}
		for i, file := range pkg.Syntax {
			abs := pkg.CompiledGoFiles[i]
			rel := relTo(params.ProjectRoot, abs)
			if rel == "" {
				continue
			}
			if len(candidateSet) > 0 && !candidateSet[rel] {
				continue
			}
			analysis := analyzeMember(pkg, file, params)
			if hasContent(analysis) {
				result.Files[rel] = analysis
			}
		}
	}
	return result, nil
}

// ModuleBinding runs project-wide rename analysis for a top-level
// binding (function, type, var, const).
func ModuleBinding(params rpc.RenameModuleBindingParams) (rpc.RenameResult, error) {
	pkgs, err := loadPackages(params.ProjectRoot)
	if err != nil {
		return rpc.RenameResult{}, err
	}

	candidateSet := stringSet(params.CandidateFiles)
	result := rpc.RenameResult{Files: map[string]rpc.RenameFileAnalysis{}}

	for _, pkg := range pkgs {
		if pkg.TypesInfo == nil {
			continue
		}
		for i, file := range pkg.Syntax {
			abs := pkg.CompiledGoFiles[i]
			rel := relTo(params.ProjectRoot, abs)
			if rel == "" {
				continue
			}
			if len(candidateSet) > 0 && !candidateSet[rel] {
				continue
			}
			analysis := analyzeModuleBinding(pkg, file, params)
			if hasContent(analysis) {
				result.Files[rel] = analysis
			}
		}
	}
	return result, nil
}

// ─── package loading ──────────────────────────────────────────────

func loadPackages(projectRoot string) ([]*packages.Package, error) {
	cfg := &packages.Config{
		Mode: packages.NeedName |
			packages.NeedFiles |
			packages.NeedCompiledGoFiles |
			packages.NeedImports |
			packages.NeedTypes |
			packages.NeedTypesInfo |
			packages.NeedSyntax |
			packages.NeedTypesSizes,
		Dir:   projectRoot,
		Tests: false,
	}
	pkgs, err := packages.Load(cfg, "./...")
	if err != nil {
		return nil, fmt.Errorf("packages.Load: %w", err)
	}
	return pkgs, nil
}

// ─── member analysis ──────────────────────────────────────────────

func analyzeMember(pkg *packages.Package, file *ast.File, params rpc.RenameMemberParams) rpc.RenameFileAnalysis {
	fset := pkg.Fset
	leaf := params.Leaf
	var out rpc.RenameFileAnalysis

	ast.Inspect(file, func(n ast.Node) bool {
		switch x := n.(type) {

		case *ast.FuncDecl:
			// Method declaration whose name matches the leaf.
			if x.Recv == nil || x.Name == nil || x.Name.Name != leaf {
				return true
			}
			recvQpath := methodReceiverQpath(pkg, x)
			if recvQpath == "" {
				return true
			}
			if recvQpath == params.TargetOwnerQpath {
				out.Rewrites = append(out.Rewrites, identRewrite(fset, file, x.Name, params.NewName, ""))
			}
			// Methods of other types with the same leaf are silently
			// ignored (not "skipped_mismatch") — they're separate
			// declarations, not refs to discriminate against.

		case *ast.SelectorExpr:
			if x.Sel == nil || x.Sel.Name != leaf {
				return true
			}
			sel, ok := pkg.TypesInfo.Selections[x]
			if !ok {
				return true
			}
			// Discriminate against the *actual method's* owner — not the
			// expression type. Critical for embedded-method promotion:
			// `b.Save()` where B embeds A and Save is promoted resolves
			// here to A.Save, not B.Save. Same for interface dispatch
			// (`s.Save()` where s is Saver resolves to Saver.Save).
			methodQpath := selObjMethodOwnerQpath(sel)
			recvSrc := nodeText(fset, file, x.X)
			pos := fset.Position(x.Sel.Pos())
			end := fset.Position(x.Sel.End())
			byteStart, byteEnd := pos.Offset, end.Offset
			line, col := pos.Line, pos.Column-1

			if methodQpath == params.TargetOwnerQpath {
				out.Rewrites = append(out.Rewrites, rpc.ByteRewrite{
					ByteStart: byteStart, ByteEnd: byteEnd,
					NewText:   params.NewName,
					Line:      line, Col: col,
					ReceiverSource: recvSrc,
				})
			} else if methodQpath != "" {
				out.SkippedMismatch = append(out.SkippedMismatch, rpc.SkippedMismatchSite{
					ByteStart: byteStart, ByteEnd: byteEnd,
					Line:      line, Col: col,
					ReceiverSource:  recvSrc,
					ResolvedToQpath: methodQpath + "." + leaf,
				})
			} else {
				out.Unresolved = append(out.Unresolved, rpc.UnresolvedSite{
					ByteStart: byteStart, ByteEnd: byteEnd,
					Line:      line, Col: col,
					ReceiverSource: recvSrc,
					Why:            "could not extract method's owning type from go/types Selection",
				})
			}
		}
		return true
	})
	return out
}

// ─── module-binding analysis ──────────────────────────────────────

func analyzeModuleBinding(pkg *packages.Package, file *ast.File, params rpc.RenameModuleBindingParams) rpc.RenameFileAnalysis {
	fset := pkg.Fset
	leaf := params.Leaf
	var out rpc.RenameFileAnalysis

	ast.Inspect(file, func(n ast.Node) bool {
		switch x := n.(type) {

		case *ast.FuncDecl:
			// Top-level function declaration (no receiver) matching leaf.
			if x.Recv != nil || x.Name == nil || x.Name.Name != leaf {
				return true
			}
			obj := pkg.TypesInfo.Defs[x.Name]
			if obj == nil || qpathOfObj(obj) != params.TargetQpath {
				return true
			}
			out.Rewrites = append(out.Rewrites, identRewrite(fset, file, x.Name, params.NewName, ""))

		case *ast.TypeSpec:
			if x.Name == nil || x.Name.Name != leaf {
				return true
			}
			obj := pkg.TypesInfo.Defs[x.Name]
			if obj == nil || qpathOfObj(obj) != params.TargetQpath {
				return true
			}
			out.Rewrites = append(out.Rewrites, identRewrite(fset, file, x.Name, params.NewName, ""))

		case *ast.ValueSpec:
			// var / const declarations may bind multiple names per spec.
			for _, name := range x.Names {
				if name.Name != leaf {
					continue
				}
				obj := pkg.TypesInfo.Defs[name]
				if obj == nil || qpathOfObj(obj) != params.TargetQpath {
					continue
				}
				out.Rewrites = append(out.Rewrites, identRewrite(fset, file, name, params.NewName, ""))
			}

		case *ast.Ident:
			// Catches both bare references (`Make()` in declaring pkg)
			// AND the Sel ident inside `pkg.Make()` cross-package access.
			// SelectorExpr handling for module-binding is intentionally
			// absent — Uses resolves the inner Sel ident directly and
			// adding a SelectorExpr case would double-count every
			// cross-package call.
			if x.Name != leaf {
				return true
			}
			obj := pkg.TypesInfo.Uses[x]
			if obj == nil {
				return true
			}
			if qpathOfObj(obj) != params.TargetQpath {
				return true
			}
			out.Rewrites = append(out.Rewrites, identRewrite(fset, file, x, params.NewName, ""))
		}
		return true
	})
	return out
}

// ─── helpers ──────────────────────────────────────────────────────

// selObjMethodOwnerQpath returns the qpath of the type that *owns* the
// method behind a Selection — not the type of the selector expression.
// For promoted methods (B embeds A, b.Save calls A.Save) this returns
// A's qpath. For interface dispatch (s Saver; s.Save) this returns
// Saver's qpath. For direct calls it returns the receiver's qpath as
// expected.
func selObjMethodOwnerQpath(sel *types.Selection) string {
	obj := sel.Obj()
	fn, ok := obj.(*types.Func)
	if !ok {
		// Field selection, not a method. For tier-1 we don't rename
		// fields via this path — surface as no-match.
		return ""
	}
	sig, ok := fn.Type().(*types.Signature)
	if !ok {
		return ""
	}
	recv := sig.Recv()
	if recv == nil {
		return ""
	}
	return qpathOfType(recv.Type())
}

// qpathOfType returns "<package_path>.<TypeName>" for named types,
// unwrapping a single pointer indirection. Empty string for anonymous
// or non-named types.
func qpathOfType(t types.Type) string {
	if ptr, ok := t.(*types.Pointer); ok {
		t = ptr.Elem()
	}
	named, ok := t.(*types.Named)
	if !ok {
		return ""
	}
	obj := named.Obj()
	if obj == nil {
		return ""
	}
	pkg := obj.Pkg()
	if pkg == nil {
		return obj.Name() // builtin
	}
	return pkg.Path() + "." + obj.Name()
}

// qpathOfObj returns the qualified path for a top-level types.Object —
// "<package_path>.<Name>" for package-level decls; "" for locals.
func qpathOfObj(obj types.Object) string {
	pkg := obj.Pkg()
	if pkg == nil {
		return ""
	}
	// Restrict to package-level objects — locals don't have stable qpaths
	// and aren't candidates for module-binding rename.
	if obj.Parent() != pkg.Scope() {
		return ""
	}
	return pkg.Path() + "." + obj.Name()
}

// methodReceiverQpath extracts the receiver-type qpath from a FuncDecl.
func methodReceiverQpath(pkg *packages.Package, d *ast.FuncDecl) string {
	if d.Recv == nil || len(d.Recv.List) == 0 {
		return ""
	}
	recvField := d.Recv.List[0]
	obj := pkg.TypesInfo.Defs[d.Name]
	if obj == nil {
		// Fall back to AST inspection.
		return recvAstQpath(pkg, recvField.Type)
	}
	fn, ok := obj.(*types.Func)
	if !ok {
		return ""
	}
	sig, ok := fn.Type().(*types.Signature)
	if !ok {
		return ""
	}
	recvVar := sig.Recv()
	if recvVar == nil {
		return ""
	}
	return qpathOfType(recvVar.Type())
}

func recvAstQpath(pkg *packages.Package, expr ast.Expr) string {
	switch t := expr.(type) {
	case *ast.StarExpr:
		return recvAstQpath(pkg, t.X)
	case *ast.Ident:
		obj := pkg.TypesInfo.Uses[t]
		if obj == nil {
			obj = pkg.TypesInfo.Defs[t]
		}
		if obj == nil {
			return ""
		}
		if obj.Pkg() == nil {
			return obj.Name()
		}
		return obj.Pkg().Path() + "." + obj.Name()
	}
	return ""
}

func identRewrite(fset *token.FileSet, _ *ast.File, ident *ast.Ident, newText, recv string) rpc.ByteRewrite {
	pos := fset.Position(ident.Pos())
	end := fset.Position(ident.End())
	return rpc.ByteRewrite{
		ByteStart:      pos.Offset,
		ByteEnd:        end.Offset,
		NewText:        newText,
		Line:           pos.Line,
		Col:            pos.Column - 1,
		ReceiverSource: recv,
	}
}

func nodeText(fset *token.FileSet, file *ast.File, n ast.Node) string {
	if n == nil {
		return ""
	}
	start := fset.Position(n.Pos())
	end := fset.Position(n.End())
	if start.Filename != end.Filename || start.Offset >= end.Offset {
		return ""
	}
	// We don't have the raw bytes here; reconstruct from token-stream
	// best-effort. For receiver source most cases are short Ident-like
	// expressions, so a printer-formatted version is fine.
	// Returning empty is acceptable — receiver_source is informational.
	_ = file
	return ""
}

func relTo(root, abs string) string {
	rel, err := filepath.Rel(root, abs)
	if err != nil {
		return ""
	}
	return rel
}

func stringSet(xs []string) map[string]bool {
	out := map[string]bool{}
	for _, x := range xs {
		out[x] = true
	}
	return out
}

func hasContent(a rpc.RenameFileAnalysis) bool {
	return len(a.Rewrites) > 0 || len(a.SkippedMismatch) > 0 || len(a.Unresolved) > 0
}
