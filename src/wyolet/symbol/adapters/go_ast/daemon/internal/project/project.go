// Package project loads a Go project with full type information, test
// files included, and keeps the result for later calls in the same process
// until a Go source or module file under the root changes.
//
// The go command runs with the caller's environment from the project root,
// so it selects the toolchain the project's go.mod / go.work asks for.
package project

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"go/ast"
	"go/token"
	"go/types"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"slices"
	"sort"
	"strings"
	"sync"

	"golang.org/x/tools/go/packages"
)

// Project is one load of every package under Root.
type Project struct {
	Root string
	Fset *token.FileSet
	// Packages holds project packages and their test variants, minus the
	// synthesized test mains. One file can appear in several variants.
	Packages []*packages.Package
	// NotAnalyzed lists Go files under Root that no loaded package
	// type-checks (build constraints, modules outside the load).
	NotAnalyzed []string
	errors      []string
	stamp       string
}

var (
	mu     sync.Mutex
	cached *Project
)

// Load returns the project at root, reusing the previous load when no Go
// source or module file changed since. Only a failure of the go command
// itself is returned as an error; per-package errors are kept for Err.
func Load(root string) (*Project, error) {
	root, err := filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	stamp, goFiles, err := sourceStamp(root)
	if err != nil {
		return nil, err
	}
	mu.Lock()
	defer mu.Unlock()
	if cached != nil && cached.Root == root && cached.stamp == stamp {
		return cached, nil
	}
	cached = nil // release the previous load before building the next
	p, err := load(root, goFiles)
	if err != nil {
		return nil, err
	}
	p.stamp = stamp
	cached = p
	return p, nil
}

func load(root string, goFiles []string) (*Project, error) {
	mainDirs, err := mainModuleDirs(root)
	if err != nil {
		return nil, err
	}
	nested := nestedModuleDirs(root)
	p := &Project{Root: root, Fset: token.NewFileSet()}
	pkgs, err := loadFrom(root, rootPatterns(root, mainDirs, nested), p.Fset)
	if err != nil {
		return nil, err
	}
	// A nested module outside the root's build list is loaded from its own
	// directory, under its own go.mod and toolchain. Its uses of other
	// modules resolve through export data, matched by DeclSite.
	for _, dir := range nested {
		if slices.Contains(mainDirs, dir) || coversDir(pkgs, dir) {
			continue
		}
		more, err := loadFrom(dir, []string{"./..."}, p.Fset)
		if err != nil {
			return nil, err
		}
		pkgs = append(pkgs, more...)
	}
	p.Packages = pkgs
	p.errors = collectErrors(pkgs)
	p.NotAnalyzed = notAnalyzed(p, goFiles)
	return p, nil
}

func loadFrom(dir string, patterns []string, fset *token.FileSet) ([]*packages.Package, error) {
	cfg := &packages.Config{
		Mode: packages.NeedName |
			packages.NeedFiles |
			packages.NeedCompiledGoFiles |
			packages.NeedImports |
			packages.NeedTypes |
			packages.NeedTypesInfo |
			packages.NeedSyntax |
			packages.NeedTypesSizes,
		Dir:   dir,
		Fset:  fset,
		Tests: true,
	}
	pkgs, err := packages.Load(cfg, patterns...)
	if err != nil {
		return nil, classify([]string{err.Error()})
	}
	return slices.DeleteFunc(pkgs, isTestMain), nil
}

// coversDir reports whether any loaded package compiles a file under dir.
func coversDir(pkgs []*packages.Package, dir string) bool {
	for _, pkg := range pkgs {
		for _, file := range pkg.GoFiles {
			if Rel(dir, file) != "" {
				return true
			}
		}
	}
	return false
}

// isTestMain reports the synthesized `p.test` main package, whose only
// file is a generated _testmain.go outside the project.
func isTestMain(pkg *packages.Package) bool {
	return pkg.Name == "main" && strings.HasSuffix(pkg.ID, ".test")
}

// ─── what to load ─────────────────────────────────────────────────

// rootPatterns covers the modules the root's build sees: `./...` stops at
// nested go.mod files, so workspace modules get their own directory
// pattern and other nested modules (e.g. local `replace` targets) an
// import-path one, which only matches when they are in the build list.
// Loading those in the same pass keeps one object per declaration.
func rootPatterns(root string, mainDirs, nested []string) []string {
	var patterns []string
	insideMain := len(mainDirs) == 0
	for _, dir := range mainDirs {
		switch rel := Rel(root, dir); {
		case rel == "" && Rel(dir, root) != "", rel == ".":
			insideMain = true
		case rel != "":
			patterns = append(patterns, "./"+filepath.ToSlash(rel)+"/...")
		}
	}
	if insideMain {
		patterns = append([]string{"./..."}, patterns...)
	}
	for _, dir := range nested {
		if slices.Contains(mainDirs, dir) {
			continue
		}
		if path := modulePath(filepath.Join(dir, "go.mod")); path != "" {
			patterns = append(patterns, path+"/...")
		}
	}
	return patterns
}

// mainModuleDirs asks the go command for the main modules (every `use` of
// a go.work, else the enclosing module). Outside any module it returns
// nothing and loading falls back to `./...`.
func mainModuleDirs(root string) ([]string, error) {
	cmd := exec.Command("go", "list", "-m", "-f", "{{.Dir}}")
	cmd.Dir = root
	out, err := cmd.Output()
	if err != nil {
		msg := err.Error()
		if exit, ok := err.(*exec.ExitError); ok && len(exit.Stderr) > 0 {
			msg = strings.TrimSpace(string(exit.Stderr))
		}
		if strings.Contains(msg, "not using modules") || strings.Contains(msg, "go.mod file not found") {
			return nil, nil
		}
		return nil, classify([]string{"go list -m: " + msg})
	}
	var dirs []string
	for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		if line != "" {
			dirs = append(dirs, filepath.Clean(line))
		}
	}
	return dirs, nil
}

// nestedModuleDirs lists directories below root holding a go.mod, parents
// before children.
func nestedModuleDirs(root string) []string {
	var out []string
	_ = filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() {
			if path != root && skipDir(d.Name()) {
				return filepath.SkipDir
			}
			return nil
		}
		if d.Name() == "go.mod" && filepath.Dir(path) != root {
			out = append(out, filepath.Dir(path))
		}
		return nil
	})
	return out
}

func modulePath(gomod string) string {
	f, err := os.Open(gomod)
	if err != nil {
		return ""
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if rest, ok := strings.CutPrefix(line, "module"); ok {
			return strings.Trim(strings.TrimSpace(rest), `"`)
		}
	}
	return ""
}

// skipDir mirrors the directories `./...` never matches in module mode,
// plus node_modules, which is large and never holds project Go code.
func skipDir(name string) bool {
	return strings.HasPrefix(name, ".") || strings.HasPrefix(name, "_") ||
		name == "testdata" || name == "vendor" || name == "node_modules"
}

// ─── change detection ─────────────────────────────────────────────

// sourceStamp hashes the path, size and mtime of every Go source and
// module file under root, and returns the Go sources it saw.
func sourceStamp(root string) (string, []string, error) {
	h := sha256.New()
	var goFiles []string
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() {
			if path != root && skipDir(d.Name()) {
				return filepath.SkipDir
			}
			return nil
		}
		name := d.Name()
		isGo := strings.HasSuffix(name, ".go")
		switch {
		case isGo, name == "go.mod", name == "go.sum", name == "go.work", name == "go.work.sum":
		default:
			return nil
		}
		info, err := d.Info()
		if err != nil {
			return nil
		}
		fmt.Fprintf(h, "%s\x00%d\x00%d\n", path, info.Size(), info.ModTime().UnixNano())
		if isGo {
			goFiles = append(goFiles, path)
		}
		return nil
	})
	if err != nil {
		return "", nil, err
	}
	return hex.EncodeToString(h.Sum(nil)), goFiles, nil
}

func notAnalyzed(p *Project, goFiles []string) []string {
	seen := map[string]bool{}
	for _, pkg := range p.Packages {
		for _, file := range pkg.Syntax {
			seen[pkg.Fset.File(file.Pos()).Name()] = true
		}
	}
	out := []string{}
	for _, abs := range goFiles {
		if !seen[abs] {
			out = append(out, Rel(p.Root, abs))
		}
	}
	return out
}

// ─── errors ───────────────────────────────────────────────────────

// LoadError is a failed or partial load, with the loader's own messages.
type LoadError struct {
	// Code is "load_failed", "toolchain_mismatch" (the go command cannot
	// provide the Go version the project requires) or "goscan_outdated"
	// (go-scan's go/types predates the project's Go version).
	Code       string
	Message    string
	RequiredGo string
	Errors     []string
}

func (e *LoadError) Error() string { return e.Message }

const maxReportedErrors = 20

var (
	tooNewRe      = regexp.MustCompile(`requires newer Go version (go[0-9][0-9.a-z]*)`)
	toolchainRe   = regexp.MustCompile(`requires go >= ([0-9][0-9.a-z]*)`)
	noToolchainRe = regexp.MustCompile(`download go([0-9][0-9.a-z]*) .*toolchain not available`)
)

func classify(errs []string) *LoadError {
	e := &LoadError{Code: "load_failed", Errors: errs}
	if len(e.Errors) > maxReportedErrors {
		e.Errors = e.Errors[:maxReportedErrors]
	}
	for _, msg := range errs {
		if m := tooNewRe.FindStringSubmatch(msg); m != nil {
			e.Code, e.RequiredGo = "goscan_outdated", m[1]
			e.Message = fmt.Sprintf(
				"the project requires %s but go-scan was built with %s; rebuild go-scan with Go %s or newer",
				m[1], runtime.Version(), strings.TrimPrefix(m[1], "go"))
			return e
		}
		m := toolchainRe.FindStringSubmatch(msg)
		if m == nil {
			m = noToolchainRe.FindStringSubmatch(msg)
		}
		if m != nil {
			e.Code, e.RequiredGo = "toolchain_mismatch", "go"+m[1]
			e.Message = "the go command cannot provide the Go version the project requires: " + msg
			return e
		}
	}
	e.Message = fmt.Sprintf("loading Go packages failed with %d error(s); first: %s", len(errs), errs[0])
	return e
}

// collectErrors gathers the errors of every loaded package and its
// dependencies, each message once, in a stable order.
func collectErrors(pkgs []*packages.Package) []string {
	seen := map[string]bool{}
	packages.Visit(pkgs, nil, func(pkg *packages.Package) {
		for _, e := range pkg.Errors {
			seen[e.Error()] = true
		}
	})
	out := make([]string, 0, len(seen))
	for msg := range seen {
		out = append(out, msg)
	}
	sort.Strings(out)
	return out
}

// Err reports every loader error (go list, parse, type) as a *LoadError.
// Results computed from a project with errors may silently miss sites.
func (p *Project) Err() error {
	if len(p.errors) == 0 {
		return nil
	}
	return classify(p.errors)
}

// VersionErr reports only toolchain and go-scan version failures, under
// which nothing type-checks at all.
func (p *Project) VersionErr() error {
	if err := p.Err(); err != nil && err.(*LoadError).Code != "load_failed" {
		return err
	}
	return nil
}

// ─── traversal and target identity ────────────────────────────────

// ForEachFile visits every type-checked project file once per package
// variant that compiles it. A non-empty `only` restricts the visit to
// those root-relative paths.
func (p *Project) ForEachFile(only []string, visit func(pkg *packages.Package, file *ast.File, rel string)) {
	filter := map[string]bool{}
	for _, rel := range only {
		filter[rel] = true
	}
	for _, pkg := range p.Packages {
		if pkg.TypesInfo == nil {
			continue
		}
		for _, file := range pkg.Syntax {
			rel := Rel(p.Root, pkg.Fset.File(file.Pos()).Name())
			if rel == "" || (len(filter) > 0 && !filter[rel]) {
				continue
			}
			visit(pkg, file, rel)
		}
	}
}

// DeclSite is where a declaration is written. Every package variant that
// type-checks the declaring file yields its own types.Object for it, but
// all of them share this site. It is keyed by line and name rather than
// offset because objects read from export data (another module's load)
// carry only file and line.
type DeclSite struct {
	File string
	Line int
	Name string
}

func SiteOf(fset *token.FileSet, obj types.Object) DeclSite {
	pos := fset.Position(obj.Pos())
	return DeclSite{pos.Filename, pos.Line, obj.Name()}
}

func (d DeclSite) Matches(fset *token.FileSet, obj types.Object) bool {
	if obj == nil || !obj.Pos().IsValid() {
		return false
	}
	return SiteOf(fset, obj) == d
}

// FindDecl returns the object declared as `leaf` inside `span` of the
// declaring file, as type-checked by the first variant that compiles it.
func (p *Project) FindDecl(declaringFile string, span [2]int, leaf string, accept func(types.Object) bool) types.Object {
	var found types.Object
	p.ForEachFile([]string{declaringFile}, func(pkg *packages.Package, file *ast.File, _ string) {
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

// NamedInImports finds the type declared at `site` within pkg or its
// transitive imports, i.e. the variant of that type pkg compiles against.
func NamedInImports(pkg *packages.Package, name string, site DeclSite) *types.Named {
	visited := map[*packages.Package]bool{}
	var walk func(p *packages.Package) *types.Named
	walk = func(p *packages.Package) *types.Named {
		if visited[p] {
			return nil
		}
		visited[p] = true
		if p.Types != nil {
			if tn, ok := p.Types.Scope().Lookup(name).(*types.TypeName); ok && site.Matches(pkg.Fset, tn) {
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

// Rel returns abs relative to root, or "" when abs lies outside root
// (generated files in the build cache, e.g. cgo or test mains).
func Rel(root, abs string) string {
	rel, err := filepath.Rel(root, abs)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return ""
	}
	return rel
}
