// Package rename implements project-wide rename analysis for Go using
// go/types via golang.org/x/tools/go/packages. Loads the project once,
// classifies every reference site (SelectorExpr.Sel matching the leaf,
// FuncDecl.Name matching the leaf) against the rename target using
// semantic-correct receiver resolution.
//
// Unlike a tier-1 AST-only resolver, this handles interface dispatch,
// embedded methods, generics, and factory-returned receivers correctly
// via info.Selections[sel].Recv().
//
// Test files are loaded too, so one source file can be type-checked in
// several package variants (p, p [p.test], p_test [p.test]). The target
// is therefore identified by its declaration position, not by object.
package rename

import (
	"cmp"
	"fmt"
	"go/ast"
	"go/token"
	"go/types"
	"path/filepath"
	"slices"
	"strings"

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
	result := rpc.RenameResult{Files: map[string]rpc.RenameFileAnalysis{}}

	decl := findDecl(pkgs, params.ProjectRoot, params.DeclaringFile, params.DeclByteRange, params.Leaf, isMethod)
	if decl == nil {
		return result, nil
	}
	target := newDeclSite(pkgs[0].Fset, decl)

	forEachFile(pkgs, params.ProjectRoot, params.CandidateFiles, func(pkg *packages.Package, file *ast.File, rel string) {
		mergeFile(result.Files, rel, analyzeMember(pkg, file, params, target))
	})
	finalize(result.Files)

	// Project-wide check: which interfaces does the target type satisfy
	// that have a method with the same leaf? Renaming the method leaves
	// those contracts unsatisfied. We surface but never auto-rename.
	affected := findAffectedInterfaces(pkgs, decl.(*types.Func), params.Leaf, params.ProjectRoot)
	if len(affected) > 0 {
		// Stamp on the declaring file so the per-file protocol surfaces
		// them; the Python engine deduplicates across files.
		analysis := result.Files[params.DeclaringFile]
		analysis.AffectedInterfaces = affected
		result.Files[params.DeclaringFile] = analysis
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
	result := rpc.RenameResult{Files: map[string]rpc.RenameFileAnalysis{}}

	decl := findDecl(pkgs, params.ProjectRoot, params.DeclaringFile, params.DeclByteRange, params.Leaf, isPackageLevel)
	if decl == nil {
		return result, nil
	}
	target := newDeclSite(pkgs[0].Fset, decl)

	forEachFile(pkgs, params.ProjectRoot, params.CandidateFiles, func(pkg *packages.Package, file *ast.File, rel string) {
		mergeFile(result.Files, rel, analyzeModuleBinding(pkg, file, params, target))
	})
	finalize(result.Files)
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
		Tests: true,
	}
	pkgs, err := packages.Load(cfg, "./...")
	if err != nil {
		return nil, fmt.Errorf("packages.Load: %w", err)
	}
	return slices.DeleteFunc(pkgs, isTestMain), nil
}

// isTestMain reports the synthesized `p.test` main package, whose only
// file is a generated _testmain.go outside the project.
func isTestMain(pkg *packages.Package) bool {
	return pkg.Name == "main" && strings.HasSuffix(pkg.ID, ".test")
}

// forEachFile visits every type-checked project file once per package
// variant that compiles it.
func forEachFile(
	pkgs []*packages.Package, projectRoot string, candidates []string,
	visit func(pkg *packages.Package, file *ast.File, rel string),
) {
	candidateSet := stringSet(candidates)
	for _, pkg := range pkgs {
		if pkg.TypesInfo == nil {
			continue
		}
		for _, file := range pkg.Syntax {
			rel := relTo(projectRoot, pkg.Fset.File(file.Pos()).Name())
			if rel == "" {
				continue
			}
			if len(candidateSet) > 0 && !candidateSet[rel] {
				continue
			}
			visit(pkg, file, rel)
		}
	}
}

// ─── target identity ──────────────────────────────────────────────

// declSite is where a declaration is written. Every package variant
// that type-checks the declaring file yields its own types.Object for
// it, but all of them share this position.
type declSite struct {
	file   string
	offset int
}

func newDeclSite(fset *token.FileSet, obj types.Object) declSite {
	p := fset.Position(obj.Pos())
	return declSite{p.Filename, p.Offset}
}

func (d declSite) matches(fset *token.FileSet, obj types.Object) bool {
	if obj == nil || !obj.Pos().IsValid() {
		return false
	}
	return newDeclSite(fset, obj) == d
}

// findDecl returns the object declared as `leaf` inside `span` of the
// declaring file, as type-checked by the first variant that compiles it.
func findDecl(
	pkgs []*packages.Package, projectRoot, declaringFile string, span [2]int, leaf string,
	accept func(types.Object) bool,
) types.Object {
	var found types.Object
	forEachFile(pkgs, projectRoot, []string{declaringFile}, func(pkg *packages.Package, file *ast.File, _ string) {
		if found != nil {
			return
		}
		ast.Inspect(file, func(n ast.Node) bool {
			id, ok := n.(*ast.Ident)
			if found != nil || !ok || id.Name != leaf {
				return found == nil
			}
			offset := pkg.Fset.Position(id.Pos()).Offset
			if offset < span[0] || offset >= span[1] {
				return true
			}
			if obj := pkg.TypesInfo.Defs[id]; obj != nil && accept(obj) {
				found = obj
			}
			return true
		})
	})
	return found
}

func isMethod(obj types.Object) bool {
	fn, ok := obj.(*types.Func)
	return ok && fn.Signature().Recv() != nil
}

func isPackageLevel(obj types.Object) bool {
	return obj.Pkg() != nil && obj.Parent() == obj.Pkg().Scope()
}

// ─── member analysis ──────────────────────────────────────────────

func analyzeMember(pkg *packages.Package, file *ast.File, params rpc.RenameMemberParams, target declSite) rpc.RenameFileAnalysis {
	fset := pkg.Fset
	leaf := params.Leaf
	var out rpc.RenameFileAnalysis

	ast.Inspect(file, func(n ast.Node) bool {
		switch x := n.(type) {

		case *ast.FuncDecl:
			// Methods of other types with the same leaf are separate
			// declarations, not refs to discriminate against.
			if x.Recv == nil || x.Name == nil || x.Name.Name != leaf {
				return true
			}
			if target.matches(fset, pkg.TypesInfo.Defs[x.Name]) {
				out.Rewrites = append(out.Rewrites, identRewrite(fset, file, x.Name, params.NewName, ""))
			}

		case *ast.SelectorExpr:
			if x.Sel == nil || x.Sel.Name != leaf {
				return true
			}
			recvSrc := nodeText(fset, file, x.X)
			pos := fset.Position(x.Sel.Pos())
			end := fset.Position(x.Sel.End())
			byteStart, byteEnd := pos.Offset, end.Offset
			line, col := pos.Line, pos.Column-1

			sel, ok := pkg.TypesInfo.Selections[x]
			if !ok {
				// Qualified identifiers (pkg.Name) are Uses, not Selections.
				if _, used := pkg.TypesInfo.Uses[x.Sel]; !used {
					out.Unresolved = append(out.Unresolved, unresolvedSite(fset, x.Sel, recvSrc))
				}
				return true
			}
			// sel.Obj() is the method actually selected, not the expression
			// type's: promoted `b.Save()` (B embeds A) resolves to A.Save,
			// and `s.Save()` on interface Saver resolves to Saver.Save.
			ownerQpath := selObjMethodOwnerQpath(sel)
			// A struct field selection is fully resolved, just never the method.
			_, isField := sel.Obj().(*types.Var)
			if isField {
				ownerQpath = qpathOfType(sel.Recv())
			}

			if target.matches(fset, sel.Obj()) {
				out.Rewrites = append(out.Rewrites, rpc.ByteRewrite{
					ByteStart: byteStart, ByteEnd: byteEnd,
					NewText: params.NewName,
					Line:    line, Col: col,
					ReceiverSource: recvSrc,
				})
			} else if ownerQpath != "" || isField {
				out.SkippedMismatch = append(out.SkippedMismatch, rpc.SkippedMismatchSite{
					ByteStart: byteStart, ByteEnd: byteEnd,
					Line: line, Col: col,
					ReceiverSource:  recvSrc,
					ResolvedToQpath: ownerQpath + "." + leaf,
				})
			} else {
				out.Unresolved = append(out.Unresolved, rpc.UnresolvedSite{
					ByteStart: byteStart, ByteEnd: byteEnd,
					Line: line, Col: col,
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

func analyzeModuleBinding(pkg *packages.Package, file *ast.File, params rpc.RenameModuleBindingParams, target declSite) rpc.RenameFileAnalysis {
	fset := pkg.Fset
	var out rpc.RenameFileAnalysis

	// Idents cover the declaration (Defs), bare references, and the Sel
	// of `pkg.Make` (both Uses). An embedded field's type ident sits in
	// both maps: Defs holds the field, Uses holds the type.
	ast.Inspect(file, func(n ast.Node) bool {
		x, ok := n.(*ast.Ident)
		if !ok || x.Name != params.Leaf {
			return true
		}
		def, isDef := pkg.TypesInfo.Defs[x]
		use, isUse := pkg.TypesInfo.Uses[x]
		switch {
		case !isDef && !isUse:
			out.Unresolved = append(out.Unresolved, unresolvedSite(fset, x, ""))
		case target.matches(fset, use) || target.matches(fset, def):
			out.Rewrites = append(out.Rewrites, identRewrite(fset, file, x, params.NewName, ""))
		}
		return true
	})
	return out
}

// ─── helpers ──────────────────────────────────────────────────────

// findAffectedInterfaces returns interfaces whose method-set contains
// `leaf` AND that the owner of `method` satisfies. Each such interface
// means the rename will leave a contract unsatisfied — we surface but
// never silently extend the rename to cover them.
func findAffectedInterfaces(
	pkgs []*packages.Package, method *types.Func, leaf, projectRoot string,
) []rpc.AffectedInterface {
	owner := receiverNamed(method)
	if owner == nil {
		return nil
	}
	ownerSite := newDeclSite(pkgs[0].Fset, owner.Obj())

	var out []rpc.AffectedInterface
	seen := map[string]bool{}

	for _, pkg := range pkgs {
		if pkg.Types == nil {
			continue
		}
		// Each test variant has its own copy of the owner type; mixing
		// variants makes Implements fail on otherwise identical methods.
		// Packages that never import the owner see the declared one.
		targetType := namedInImports(pkg, owner.Obj().Name(), ownerSite)
		if targetType == nil {
			targetType = owner
		}
		ptrTarget := types.NewPointer(targetType)
		scope := pkg.Types.Scope()
		for _, name := range scope.Names() {
			obj := scope.Lookup(name)
			tn, ok := obj.(*types.TypeName)
			if !ok {
				continue
			}
			named, ok := tn.Type().(*types.Named)
			if !ok {
				continue
			}
			iface, ok := named.Underlying().(*types.Interface)
			if !ok {
				continue
			}
			if !interfaceHasMethod(iface, leaf) {
				continue
			}
			if !types.Implements(targetType, iface) && !types.Implements(ptrTarget, iface) {
				continue
			}
			ifaceQpath := qpathOfType(named)
			if seen[ifaceQpath] {
				continue
			}
			seen[ifaceQpath] = true
			pos := pkg.Fset.Position(named.Obj().Pos())
			out = append(out, rpc.AffectedInterface{
				InterfaceQpath: ifaceQpath,
				MethodQpath:    ifaceQpath + "." + leaf,
				File:           relTo(projectRoot, pos.Filename),
				Line:           pos.Line,
			})
		}
	}
	return out
}

// receiverNamed returns the generic-origin named type a method is
// declared on, or nil when the receiver isn't a named type.
func receiverNamed(method *types.Func) *types.Named {
	t := method.Signature().Recv().Type()
	if ptr, ok := t.(*types.Pointer); ok {
		t = ptr.Elem()
	}
	named, ok := t.(*types.Named)
	if !ok {
		return nil
	}
	return named.Origin()
}

// namedInImports finds the type declared at `site` within pkg or its
// transitive imports, i.e. the variant of that type pkg compiles against.
func namedInImports(pkg *packages.Package, name string, site declSite) *types.Named {
	visited := map[*packages.Package]bool{}
	var walk func(p *packages.Package) *types.Named
	walk = func(p *packages.Package) *types.Named {
		if visited[p] {
			return nil
		}
		visited[p] = true
		if p.Types != nil {
			if tn, ok := p.Types.Scope().Lookup(name).(*types.TypeName); ok && site.matches(pkg.Fset, tn) {
				if named, ok := tn.Type().(*types.Named); ok {
					return named
				}
			}
		}
		for _, imp := range p.Imports {
			if named := walk(imp); named != nil {
				return named
			}
		}
		return nil
	}
	return walk(pkg)
}

func interfaceHasMethod(iface *types.Interface, leaf string) bool {
	for i := 0; i < iface.NumMethods(); i++ {
		if iface.Method(i).Name() == leaf {
			return true
		}
	}
	return false
}

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

func unresolvedSite(fset *token.FileSet, ident *ast.Ident, recv string) rpc.UnresolvedSite {
	pos := fset.Position(ident.Pos())
	end := fset.Position(ident.End())
	return rpc.UnresolvedSite{
		ByteStart:      pos.Offset,
		ByteEnd:        end.Offset,
		Line:           pos.Line,
		Col:            pos.Column - 1,
		ReceiverSource: recv,
		Why:            "go/types recorded no object for this identifier (type errors or missing dependencies)",
	}
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

// relTo returns abs relative to root, or "" when abs lies outside root
// (generated files in the build cache, e.g. cgo or test mains).
func relTo(root, abs string) string {
	rel, err := filepath.Rel(root, abs)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
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

// mergeFile folds one variant's analysis of a file into the result.
// Files compiled into several variants report each site once per
// variant; a site is kept once per bucket, keyed by its byte range.
func mergeFile(files map[string]rpc.RenameFileAnalysis, rel string, a rpc.RenameFileAnalysis) {
	if len(a.Rewrites) == 0 && len(a.SkippedMismatch) == 0 && len(a.Unresolved) == 0 {
		return
	}
	dst := files[rel]
	dst.Rewrites = appendNewSites(dst.Rewrites, a.Rewrites, func(r rpc.ByteRewrite) [2]int {
		return [2]int{r.ByteStart, r.ByteEnd}
	})
	dst.SkippedMismatch = appendNewSites(dst.SkippedMismatch, a.SkippedMismatch, func(s rpc.SkippedMismatchSite) [2]int {
		return [2]int{s.ByteStart, s.ByteEnd}
	})
	dst.Unresolved = appendNewSites(dst.Unresolved, a.Unresolved, func(u rpc.UnresolvedSite) [2]int {
		return [2]int{u.ByteStart, u.ByteEnd}
	})
	files[rel] = dst
}

func appendNewSites[T any](dst, src []T, span func(T) [2]int) []T {
	seen := make(map[[2]int]bool, len(dst))
	for _, x := range dst {
		seen[span(x)] = true
	}
	for _, x := range src {
		if k := span(x); !seen[k] {
			seen[k] = true
			dst = append(dst, x)
		}
	}
	return dst
}

// finalize orders every bucket by position so output doesn't depend on
// the order variants were visited in.
func finalize(files map[string]rpc.RenameFileAnalysis) {
	for rel, a := range files {
		slices.SortFunc(a.Rewrites, func(x, y rpc.ByteRewrite) int { return cmp.Compare(x.ByteStart, y.ByteStart) })
		slices.SortFunc(a.SkippedMismatch, func(x, y rpc.SkippedMismatchSite) int { return cmp.Compare(x.ByteStart, y.ByteStart) })
		slices.SortFunc(a.Unresolved, func(x, y rpc.UnresolvedSite) int { return cmp.Compare(x.ByteStart, y.ByteStart) })
		files[rel] = a
	}
}
