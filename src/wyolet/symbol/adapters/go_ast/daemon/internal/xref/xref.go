// Package xref answers type-resolved cross-reference queries: every use of
// one declaration, and the implements relation between interfaces and
// concrete types. Targets are matched by declaration position, so the
// package variants that test files create all count as the same object.
package xref

import (
	"cmp"
	"fmt"
	"go/token"
	"go/types"
	"slices"
	"strings"

	"github.com/wyolet/symbol/go-scan/internal/project"
	"github.com/wyolet/symbol/go-scan/internal/rpc"
	"golang.org/x/tools/go/packages"
)

// QueryError is a target the query cannot answer for, with a stable code.
type QueryError struct {
	Code    string
	Message string
}

func (e *QueryError) Error() string { return e.Message }

// References lists every use of the target across the project's packages
// and their tests.
func References(params rpc.SymbolTargetParams) (rpc.ReferencesResult, error) {
	proj, obj, err := loadTarget(params)
	if err != nil {
		return rpc.ReferencesResult{}, err
	}
	site := project.SiteOf(proj.Fset, obj)
	refs := []rpc.SourceSite{}
	seen := map[rpc.SourceSite]bool{}
	for _, pkg := range proj.Packages {
		if pkg.TypesInfo == nil {
			continue
		}
		for id, used := range pkg.TypesInfo.Uses {
			if used.Name() != obj.Name() || !site.Matches(proj.Fset, used) {
				continue
			}
			ref, inProject := sourceSite(proj, id.Pos(), id.End())
			if !inProject || seen[ref] {
				continue
			}
			seen[ref] = true
			refs = append(refs, ref)
		}
	}
	slices.SortFunc(refs, func(a, b rpc.SourceSite) int {
		return cmp.Or(cmp.Compare(a.File, b.File), cmp.Compare(a.ByteStart, b.ByteStart))
	})
	return rpc.ReferencesResult{
		Target:      resolvedSymbol(proj, obj, params.QualifiedPath),
		References:  refs,
		NotAnalyzed: proj.NotAnalyzed,
	}, nil
}

// Implementers relates an interface to the project's concrete types that
// satisfy it, or a concrete type to every interface it satisfies.
func Implementers(params rpc.SymbolTargetParams) (rpc.ImplementersResult, error) {
	proj, obj, err := loadTarget(params)
	if err != nil {
		return rpc.ImplementersResult{}, err
	}
	tn, ok := obj.(*types.TypeName)
	if !ok {
		return rpc.ImplementersResult{}, &QueryError{"invalid_target",
			fmt.Sprintf("%s is a %s; implementers needs a type", params.QualifiedPath, kindOf(obj))}
	}
	variants := typeVariants(proj, tn)
	if len(variants) == 0 {
		return rpc.ImplementersResult{}, &QueryError{"invalid_target",
			fmt.Sprintf("%s is not a named, non-generic type", params.QualifiedPath)}
	}
	result := rpc.ImplementersResult{
		Target:       resolvedSymbol(proj, obj, params.QualifiedPath),
		Implementers: []rpc.Implementation{},
		Interfaces:   []rpc.Implementation{},
		NotAnalyzed:  proj.NotAnalyzed,
	}
	if types.IsInterface(tn.Type()) {
		result.Implementers = implementersOf(proj, variants)
	} else {
		result.Interfaces = interfacesOf(proj, variants)
	}
	return result, nil
}

// ─── target resolution ────────────────────────────────────────────

// loadTarget loads the project, refusing any loader error: a package that
// failed to type-check would silently drop its references.
func loadTarget(params rpc.SymbolTargetParams) (*project.Project, types.Object, error) {
	proj, err := project.Load(params.ProjectRoot)
	if err != nil {
		return nil, nil, err
	}
	if err := proj.Err(); err != nil {
		return nil, nil, err
	}
	var obj types.Object
	if params.DeclaringFile != "" {
		obj = proj.FindDecl(params.DeclaringFile, params.DeclByteRange, params.Leaf, isNamedDecl)
	} else {
		obj = lookupQualified(proj, params.QualifiedPath)
	}
	if obj == nil && params.DeclaringFile != "" && !proj.IsAnalyzed(params.DeclaringFile) {
		return nil, nil, &QueryError{"not_analyzed", fmt.Sprintf(
			"%s is declared in %s, which no loaded package type-checks (build constraints, or a module outside the load)",
			params.QualifiedPath, params.DeclaringFile)}
	}
	if obj == nil {
		return nil, nil, &QueryError{"symbol_not_found",
			fmt.Sprintf("no Go declaration found for %s", params.QualifiedPath)}
	}
	return proj, obj, nil
}

// isNamedDecl accepts what a qualified path can name: package-level
// objects, methods (concrete or interface) and struct fields.
func isNamedDecl(obj types.Object) bool {
	switch o := obj.(type) {
	case *types.Func:
		return o.Signature().Recv() != nil || o.Parent() == o.Pkg().Scope()
	case *types.Var:
		return o.IsField() || o.Parent() == o.Pkg().Scope()
	default:
		return obj.Pkg() != nil && obj.Parent() == obj.Pkg().Scope()
	}
}

// lookupQualified resolves "import/path.Name" or "import/path.Type.Member"
// among every loaded package, dependencies included. Import paths may
// contain dots, so each split after the last slash is tried, longest first.
func lookupQualified(proj *project.Project, qpath string) types.Object {
	byPath := packagesByPath(proj)
	lastSlash := strings.LastIndex(qpath, "/")
	for i := len(qpath) - 1; i > lastSlash; i-- {
		if qpath[i] != '.' {
			continue
		}
		pkg := byPath[qpath[:i]]
		if pkg == nil {
			continue
		}
		if obj := lookupMember(pkg, strings.Split(qpath[i+1:], ".")); obj != nil {
			return obj
		}
	}
	return nil
}

// packagesByPath indexes packages by import path, preferring project
// packages, and among those the plain variant over test variants.
func packagesByPath(proj *project.Project) map[string]*types.Package {
	out := map[string]*types.Package{}
	for _, pkg := range proj.Packages {
		if pkg.Types == nil {
			continue
		}
		if _, ok := out[pkg.PkgPath]; !ok || pkg.ID == pkg.PkgPath {
			out[pkg.PkgPath] = pkg.Types
		}
	}
	for _, pkg := range dependencies(proj) {
		if _, ok := out[pkg.Path()]; !ok {
			out[pkg.Path()] = pkg
		}
	}
	return out
}

// dependencies returns every package the project's types import, directly
// or not, other than project packages. go/packages links only the direct
// imports of project packages; the type-level imports reach the rest.
func dependencies(proj *project.Project) []*types.Package {
	seen := map[*types.Package]bool{}
	for _, pkg := range proj.Packages {
		if pkg.Types != nil {
			seen[pkg.Types] = true
		}
	}
	var out []*types.Package
	var walk func(pkg *types.Package)
	walk = func(pkg *types.Package) {
		for _, imp := range pkg.Imports() {
			if !seen[imp] {
				seen[imp] = true
				out = append(out, imp)
				walk(imp)
			}
		}
	}
	for _, pkg := range proj.Packages {
		if pkg.Types != nil {
			walk(pkg.Types)
		}
	}
	return out
}

func lookupMember(pkg *types.Package, names []string) types.Object {
	obj := pkg.Scope().Lookup(names[0])
	switch {
	case obj == nil || len(names) > 2:
		return nil
	case len(names) == 1:
		return obj
	}
	tn, ok := obj.(*types.TypeName)
	if !ok {
		return nil
	}
	member, _, _ := types.LookupFieldOrMethod(tn.Type(), true, pkg, names[1])
	return member
}

// ─── implements ───────────────────────────────────────────────────

// typeVariants returns every copy of the named type declared at tn's
// position, one per package variant that compiles its file. Generic types
// are skipped: Implements is undefined for them uninstantiated.
func typeVariants(proj *project.Project, tn *types.TypeName) []*types.Named {
	site := project.SiteOf(proj.Fset, tn)
	var out []*types.Named
	add := func(obj types.Object) {
		t, ok := obj.(*types.TypeName)
		if !ok || t.IsAlias() || !site.Matches(proj.Fset, t) {
			return
		}
		named, ok := t.Type().(*types.Named)
		if ok && named.TypeParams().Len() == 0 && !slices.Contains(out, named) {
			out = append(out, named)
		}
	}
	add(tn)
	for _, pkg := range proj.Packages {
		if pkg.Types != nil {
			add(pkg.Types.Scope().Lookup(tn.Name()))
		}
	}
	return out
}

// variantFinder memoizes, per package, the variant of a type that package
// compiles against: the one in its own scope or its transitive imports.
// A build never contains two variants of one package, so any hit is it.
type variantFinder struct {
	fset *token.FileSet
	name string
	site project.DeclSite
	memo map[*packages.Package]*types.Named
}

func newVariantFinder(proj *project.Project, named *types.Named) *variantFinder {
	return &variantFinder{
		fset: proj.Fset,
		name: named.Obj().Name(),
		site: project.SiteOf(proj.Fset, named.Obj()),
		memo: map[*packages.Package]*types.Named{},
	}
}

func (v *variantFinder) in(pkg *packages.Package) *types.Named {
	if found, ok := v.memo[pkg]; ok {
		return found
	}
	v.memo[pkg] = nil
	var found *types.Named
	if pkg.Types != nil {
		if tn, ok := pkg.Types.Scope().Lookup(v.name).(*types.TypeName); ok && v.site.Matches(v.fset, tn) {
			found, _ = tn.Type().(*types.Named)
		}
	}
	for _, imp := range pkg.Imports {
		if found != nil {
			break
		}
		found = v.in(imp)
	}
	v.memo[pkg] = found
	return found
}

// candidates are the variants to test against from pkg: the one pkg sees,
// else all of them (pkg is unrelated to the declaring package).
func (v *variantFinder) candidates(pkg *packages.Package, all []*types.Named) []*types.Named {
	if found := v.in(pkg); found != nil {
		return []*types.Named{found}
	}
	return all
}

// satisfies reports whether concrete satisfies iface by value, else by
// pointer only, else not at all.
func satisfies(concrete *types.Named, iface *types.Interface) (ok, pointer bool) {
	if types.Implements(concrete, iface) {
		return true, false
	}
	if types.Implements(types.NewPointer(concrete), iface) {
		return true, true
	}
	return false, false
}

// implementersOf lists the concrete project types satisfying the
// interface, checking each against the interface variant its package sees.
func implementersOf(proj *project.Project, ifaces []*types.Named) []rpc.Implementation {
	finder := newVariantFinder(proj, ifaces[0])
	seen := map[project.DeclSite]bool{}
	out := []rpc.Implementation{}
	for _, pkg := range proj.Packages {
		if pkg.Types == nil {
			continue
		}
		scope := pkg.Types.Scope()
		for _, name := range scope.Names() {
			concrete := declaredNamed(scope.Lookup(name))
			if concrete == nil || types.IsInterface(concrete) {
				continue
			}
			site := project.SiteOf(proj.Fset, concrete.Obj())
			if seen[site] {
				continue
			}
			for _, iface := range finder.candidates(pkg, ifaces) {
				if ok, pointer := satisfies(concrete, iface.Underlying().(*types.Interface)); ok {
					seen[site] = true
					out = append(out, implementation(proj, concrete.Obj(), pointer))
					break
				}
			}
		}
	}
	sortImplementations(out)
	return out
}

// interfacesOf lists the non-empty interfaces the concrete type satisfies:
// any declared in the project, and exported ones from dependencies.
func interfacesOf(proj *project.Project, concretes []*types.Named) []rpc.Implementation {
	finder := newVariantFinder(proj, concretes[0])
	seen := map[project.DeclSite]bool{}
	out := []rpc.Implementation{}
	check := func(scope *types.Scope, candidates []*types.Named) {
		for _, name := range scope.Names() {
			named := declaredNamed(scope.Lookup(name))
			if named == nil {
				continue
			}
			iface, ok := named.Underlying().(*types.Interface)
			if !ok || iface.NumMethods() == 0 {
				continue
			}
			site := project.SiteOf(proj.Fset, named.Obj())
			external := project.Rel(proj.Root, site.File) == ""
			if seen[site] || (external && !named.Obj().Exported()) {
				continue
			}
			for _, concrete := range candidates {
				if ok, pointer := satisfies(concrete, iface); ok {
					seen[site] = true
					out = append(out, implementation(proj, named.Obj(), pointer))
					break
				}
			}
		}
	}
	for _, pkg := range proj.Packages {
		if pkg.Types != nil {
			check(pkg.Types.Scope(), finder.candidates(pkg, concretes))
		}
	}
	for _, dep := range dependencies(proj) {
		check(dep.Scope(), concretes)
	}
	sortImplementations(out)
	return out
}

// declaredNamed returns the non-generic named type obj declares, or nil.
func declaredNamed(obj types.Object) *types.Named {
	tn, ok := obj.(*types.TypeName)
	if !ok || tn.IsAlias() {
		return nil
	}
	named, ok := tn.Type().(*types.Named)
	if !ok || named.TypeParams().Len() > 0 {
		return nil
	}
	return named
}

func implementation(proj *project.Project, tn *types.TypeName, pointer bool) rpc.Implementation {
	return rpc.Implementation{
		ResolvedSymbol: resolvedSymbol(proj, tn, tn.Pkg().Path()+"."+tn.Name()),
		Pointer:        pointer,
	}
}

func sortImplementations(xs []rpc.Implementation) {
	slices.SortFunc(xs, func(a, b rpc.Implementation) int {
		if a.External != b.External {
			if a.External {
				return 1
			}
			return -1
		}
		return cmp.Compare(a.QualifiedPath, b.QualifiedPath)
	})
}

// ─── output ───────────────────────────────────────────────────────

func resolvedSymbol(proj *project.Project, obj types.Object, qpath string) rpc.ResolvedSymbol {
	site, inProject := sourceSite(proj, obj.Pos(), obj.Pos()+token.Pos(len(obj.Name())))
	return rpc.ResolvedSymbol{
		QualifiedPath: qpath,
		Kind:          kindOf(obj),
		External:      !inProject,
		SourceSite:    site,
	}
}

// sourceSite locates [pos, end), with a root-relative file when it lies
// inside the project.
func sourceSite(proj *project.Project, pos, end token.Pos) (rpc.SourceSite, bool) {
	start := proj.Fset.Position(pos)
	file := start.Filename
	rel := project.Rel(proj.Root, file)
	if rel != "" {
		file = rel
	}
	return rpc.SourceSite{
		File:      file,
		Line:      start.Line,
		Col:       start.Column,
		ByteStart: start.Offset,
		ByteEnd:   start.Offset + int(end-pos),
	}, rel != ""
}

func kindOf(obj types.Object) string {
	switch o := obj.(type) {
	case *types.Func:
		if o.Signature().Recv() != nil {
			return "method"
		}
		return "func"
	case *types.TypeName:
		if types.IsInterface(o.Type()) {
			return "interface"
		}
		return "type"
	case *types.Var:
		if o.IsField() {
			return "field"
		}
		return "var"
	case *types.Const:
		return "const"
	}
	return "object"
}
