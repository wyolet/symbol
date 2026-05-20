// Package scan walks a Go source file with go/ast and produces a FileScan
// matching schemas/symbol.rpc.schema.json. Symbol kinds, qualified paths,
// and ref classification are Go-specific; the output shape is universal.
package scan

import (
	"go/ast"
	"go/parser"
	"go/token"
	"sort"

	"github.com/wyolet/symbol/go-scan/internal/rpc"
)

// File parses `source` as Go and returns its FileScan. modulePrefix is the
// package import path (e.g. "github.com/wyolet/x/pkg/user"); empty is fine
// for one-off files outside any module.
//
// path is used only for diagnostics — bytes come from `source` so the
// daemon doesn't re-read disk and the result is reproducible.
func File(path, source, modulePrefix string) rpc.FileScan {
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, path, source, parser.SkipObjectResolution|parser.ParseComments)
	if err != nil {
		msg := err.Error()
		return rpc.FileScan{Language: "go", OK: false, Error: &msg}
	}

	scan := rpc.FileScan{
		Language: "go",
		OK:       true,
		Imports:  extractImports(f, fset),
		Symbols:  extractSymbols(f, fset, modulePrefix),
	}
	return scan
}

// ── imports ────────────────────────────────────────────────────────

func extractImports(f *ast.File, fset *token.FileSet) []rpc.ScannedImport {
	out := make([]rpc.ScannedImport, 0, len(f.Imports))
	for _, spec := range f.Imports {
		source := unquote(spec.Path.Value)
		local := defaultImportLocal(spec, source)
		out = append(out, rpc.ScannedImport{
			Local:  local,
			Source: source,
			Line:   fset.Position(spec.Pos()).Line,
		})
	}
	return out
}

// defaultImportLocal returns the name a Go file uses to refer to an
// import. With an explicit alias (``f "fmt"``) that's the alias; with
// `.` or `_` it's that punctuation (callers can treat them specially);
// otherwise it's the last path segment (``fmt``, not ``encoding/json``
// → ``json``). We don't resolve a package's declared package name —
// that would require parsing the imported package, which the v1 worker
// doesn't do.
func defaultImportLocal(spec *ast.ImportSpec, source string) string {
	if spec.Name != nil {
		return spec.Name.Name
	}
	// Last path segment.
	for i := len(source) - 1; i >= 0; i-- {
		if source[i] == '/' {
			return source[i+1:]
		}
	}
	return source
}

// ── symbols ────────────────────────────────────────────────────────

func extractSymbols(f *ast.File, fset *token.FileSet, modulePrefix string) []rpc.ScannedSymbol {
	out := make([]rpc.ScannedSymbol, 0, len(f.Decls))
	for _, decl := range f.Decls {
		switch d := decl.(type) {
		case *ast.FuncDecl:
			out = append(out, funcDeclToSymbol(d, fset, modulePrefix))
		case *ast.GenDecl:
			out = append(out, genDeclToSymbols(d, fset, modulePrefix)...)
		}
	}
	return out
}

func funcDeclToSymbol(d *ast.FuncDecl, fset *token.FileSet, modulePrefix string) rpc.ScannedSymbol {
	kind := "function"
	qualified := joinQualified(modulePrefix, d.Name.Name)
	if d.Recv != nil && len(d.Recv.List) > 0 {
		kind = "method"
		recv := receiverTypeName(d.Recv.List[0])
		if recv != "" {
			qualified = joinQualified(modulePrefix, recv, d.Name.Name)
		}
	}
	startPos := fset.Position(d.Pos())
	endPos := fset.Position(d.End())
	sym := rpc.ScannedSymbol{
		Kind:          kind,
		Name:          d.Name.Name,
		QualifiedPath: qualified,
		ByteRange:     [2]int{startPos.Offset, endPos.Offset},
		LineRange:     [2]int{startPos.Line, endPos.Line},
		Refs:          extractRefs(d, fset, declarationNames(d)),
		Children:      []rpc.ScannedSymbol{},
	}
	return sym
}

// receiverTypeName extracts the base type name from a method receiver,
// stripping pointer (`*T` → `T`) and generic instantiation (`T[U]` → `T`).
func receiverTypeName(field *ast.Field) string {
	expr := field.Type
	for {
		switch e := expr.(type) {
		case *ast.StarExpr:
			expr = e.X
		case *ast.IndexExpr:
			expr = e.X
		case *ast.IndexListExpr:
			expr = e.X
		case *ast.Ident:
			return e.Name
		default:
			return ""
		}
	}
}

// genDeclToSymbols handles `type`, `const`, and `var` declarations. Each
// spec inside a parenthesized block (``var ( a int; b string )``) becomes
// its own symbol — the declaration line ranges to the keyword for
// readability.
func genDeclToSymbols(d *ast.GenDecl, fset *token.FileSet, modulePrefix string) []rpc.ScannedSymbol {
	var kind string
	switch d.Tok {
	case token.TYPE:
		kind = "type"
	case token.CONST:
		kind = "const"
	case token.VAR:
		kind = "var"
	case token.IMPORT:
		return nil // handled separately in extractImports
	default:
		return nil
	}

	out := []rpc.ScannedSymbol{}
	for _, spec := range d.Specs {
		switch s := spec.(type) {
		case *ast.TypeSpec:
			out = append(out, simpleSymbol(kind, s.Name.Name, s.Pos(), specEnd(s), fset, modulePrefix))
		case *ast.ValueSpec:
			for _, name := range s.Names {
				if name.Name == "_" {
					continue
				}
				out = append(out, simpleSymbol(kind, name.Name, name.Pos(), specEnd(s), fset, modulePrefix))
			}
		}
	}
	return out
}

func specEnd(spec ast.Node) token.Pos {
	return spec.End()
}

func simpleSymbol(kind, name string, start, end token.Pos, fset *token.FileSet, modulePrefix string) rpc.ScannedSymbol {
	startPos := fset.Position(start)
	endPos := fset.Position(end)
	return rpc.ScannedSymbol{
		Kind:          kind,
		Name:          name,
		QualifiedPath: joinQualified(modulePrefix, name),
		ByteRange:     [2]int{startPos.Offset, endPos.Offset},
		LineRange:     [2]int{startPos.Line, endPos.Line},
		Refs:          []rpc.ScannedRef{},
		Children:      []rpc.ScannedSymbol{},
	}
}

// ── refs ───────────────────────────────────────────────────────────

// extractRefs walks a function body collecting name references. Locals
// declared inside the body (via :=, var, parameters) are filtered out
// so the resulting list approximates "names this body depends on."
// This is the Go analog of the Python adapter's ref filter.
func extractRefs(fn *ast.FuncDecl, fset *token.FileSet, skip map[string]bool) []rpc.ScannedRef {
	if fn.Body == nil {
		return []rpc.ScannedRef{}
	}

	locals := collectLocals(fn)
	for name := range locals {
		skip[name] = true
	}

	type key struct {
		name string
		kind string
		line int
	}
	seen := map[key]bool{}
	var refs []rpc.ScannedRef

	addRef := func(name, kind string, pos token.Pos) {
		if name == "" || name == "_" || skip[name] {
			return
		}
		line := fset.Position(pos).Line
		k := key{name, kind, line}
		if seen[k] {
			return
		}
		seen[k] = true
		refs = append(refs, rpc.ScannedRef{Name: name, Kind: kind, Line: line})
	}

	// Pre-collect positions of selector tails (the .Sel side of every
	// SelectorExpr). The main walk records those as "attr" and the Ident
	// branch must skip the same identifier so it isn't double-counted as
	// "name". Without this, ``x.y`` would record y as both attr and name.
	selTails := map[token.Pos]bool{}
	ast.Inspect(fn.Body, func(n ast.Node) bool {
		if se, ok := n.(*ast.SelectorExpr); ok && se.Sel != nil {
			selTails[se.Sel.Pos()] = true
		}
		return true
	})

	ast.Inspect(fn.Body, func(n ast.Node) bool {
		switch x := n.(type) {
		case *ast.SelectorExpr:
			if x.Sel != nil {
				addRef(x.Sel.Name, "attr", x.Sel.Pos())
			}
			// Descend so chains like ``a.b.c`` record b and c as attrs and
			// a as a name via the Ident branch below.
			return true
		case *ast.Ident:
			if selTails[x.Pos()] {
				// Already recorded as attr — don't double-count as name.
				return false
			}
			addRef(x.Name, "name", x.Pos())
			return false
		}
		return true
	})

	sortRefs(refs)
	return refs
}

// declarationNames returns names the function itself defines (its own
// name, receiver name, parameter names, result names). Used to seed the
// ref-skip set so a function's signature doesn't show up in its own refs.
func declarationNames(fn *ast.FuncDecl) map[string]bool {
	out := map[string]bool{fn.Name.Name: true}
	if fn.Recv != nil {
		for _, f := range fn.Recv.List {
			for _, n := range f.Names {
				out[n.Name] = true
			}
		}
	}
	if fn.Type.Params != nil {
		for _, f := range fn.Type.Params.List {
			for _, n := range f.Names {
				out[n.Name] = true
			}
		}
	}
	if fn.Type.Results != nil {
		for _, f := range fn.Type.Results.List {
			for _, n := range f.Names {
				out[n.Name] = true
			}
		}
	}
	return out
}

// collectLocals walks a function body and gathers names bound by `:=`,
// `var`, or `const` declarations. Used to filter refs.
func collectLocals(fn *ast.FuncDecl) map[string]bool {
	out := map[string]bool{}
	if fn.Body == nil {
		return out
	}
	ast.Inspect(fn.Body, func(n ast.Node) bool {
		switch x := n.(type) {
		case *ast.AssignStmt:
			if x.Tok == token.DEFINE {
				for _, lhs := range x.Lhs {
					if id, ok := lhs.(*ast.Ident); ok {
						out[id.Name] = true
					}
				}
			}
		case *ast.ValueSpec:
			for _, name := range x.Names {
				out[name.Name] = true
			}
		case *ast.RangeStmt:
			if id, ok := x.Key.(*ast.Ident); ok {
				out[id.Name] = true
			}
			if id, ok := x.Value.(*ast.Ident); ok {
				out[id.Name] = true
			}
		}
		return true
	})
	return out
}

func sortRefs(refs []rpc.ScannedRef) {
	sort.Slice(refs, func(i, j int) bool {
		if refs[i].Line != refs[j].Line {
			return refs[i].Line < refs[j].Line
		}
		if refs[i].Name != refs[j].Name {
			return refs[i].Name < refs[j].Name
		}
		return refs[i].Kind < refs[j].Kind
	})
}

// ── helpers ────────────────────────────────────────────────────────

func joinQualified(parts ...string) string {
	out := ""
	for _, p := range parts {
		if p == "" {
			continue
		}
		if out == "" {
			out = p
		} else {
			out += "." + p
		}
	}
	return out
}

func unquote(s string) string {
	if len(s) >= 2 && (s[0] == '"' || s[0] == '`') && s[len(s)-1] == s[0] {
		return s[1 : len(s)-1]
	}
	return s
}
