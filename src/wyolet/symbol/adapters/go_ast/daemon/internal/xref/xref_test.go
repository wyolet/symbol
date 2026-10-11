package xref

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"testing"

	"github.com/wyolet/symbol/go-scan/internal/project"
	"github.com/wyolet/symbol/go-scan/internal/rpc"
	"github.com/wyolet/symbol/go-scan/internal/scan"
)

// The root module reaches `sub` through a local replace; `tool` is a
// separate module outside the root's build list that imports the root.
var fixture = map[string]string{
	"go.mod": `module example.com/m

go 1.22

require example.com/m/sub v0.0.0

replace example.com/m/sub => ./sub
`,
	"lib/lib.go": `package lib

import (
	"fmt"

	"example.com/m/sub"
)

type Store struct{ Name string }

func (s *Store) Save() error { return nil }

type Value struct{}

func (Value) Save() error { return nil }

type Saver interface{ Save() error }

func Use(s Saver) error { return s.Save() }

func Exported() string { return sub.Greet() }

const Limit = 3

type Named struct{}

func (Named) String() string { return fmt.Sprint("named") }

func Build() *Store {
	st := &Store{Name: "x"}
	_ = st.Save()
	return st
}
`,
	"lib/lib_test.go": `package lib

import "testing"

func TestStore(t *testing.T) {
	s := &Store{}
	_ = s.Save()
	_ = s.Name
	_ = Limit
}
`,
	"lib/ext_test.go": `package lib_test

import (
	"testing"

	"example.com/m/lib"
)

type saver interface{ Save() error }

type extSaver struct{}

func (*extSaver) Save() error { return nil }

func TestExt(t *testing.T) {
	var s saver = &lib.Store{}
	_ = s
	_ = lib.Exported()
	_ = (&lib.Store{}).Save()
}
`,
	"sub/go.mod": "module example.com/m/sub\n\ngo 1.22\n",
	"sub/sub.go": `package sub

func Greet() string { return "hi" }
`,
	"sub/sub_test.go": `package sub

import "testing"

func TestGreet(t *testing.T) { _ = Greet() }
`,
	"tool/go.mod": `module example.com/tool

go 1.22

require (
	example.com/m v0.0.0
	example.com/m/sub v0.0.0
)

replace (
	example.com/m => ../
	example.com/m/sub => ../sub
)
`,
	"tool/main.go": `package main

import "example.com/m/lib"

func main() {
	_ = lib.Exported()
	_ = &lib.Store{}
}
`,
}

func writeProject(t *testing.T, files map[string]string) string {
	t.Helper()
	root := t.TempDir()
	for rel, src := range files {
		abs := filepath.Join(root, rel)
		if err := os.MkdirAll(filepath.Dir(abs), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(abs, []byte(src), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

// indexed targets a declaration the way the index does: by the byte
// range the scanner records for it.
func indexed(t *testing.T, root, rel, name string) rpc.SymbolTargetParams {
	t.Helper()
	src, err := os.ReadFile(filepath.Join(root, rel))
	if err != nil {
		t.Fatal(err)
	}
	for _, sym := range scan.File(rel, string(src), "").Symbols {
		if sym.Name == name {
			return rpc.SymbolTargetParams{
				ProjectRoot: root, QualifiedPath: name, Leaf: name,
				DeclaringFile: rel, DeclByteRange: sym.ByteRange,
			}
		}
	}
	t.Fatalf("%s not declared in %s", name, rel)
	return rpc.SymbolTargetParams{}
}

func byPath(root, qpath string) rpc.SymbolTargetParams {
	return rpc.SymbolTargetParams{ProjectRoot: root, QualifiedPath: qpath}
}

func refSites(t *testing.T, params rpc.SymbolTargetParams) []string {
	t.Helper()
	res, err := References(params)
	if err != nil {
		t.Fatal(err)
	}
	if len(res.NotAnalyzed) > 0 {
		t.Errorf("not analyzed: %v", res.NotAnalyzed)
	}
	var out []string
	for _, r := range res.References {
		out = append(out, fmt.Sprintf("%s:%d", r.File, r.Line))
	}
	return out
}

func assertSites(t *testing.T, got, want []string) {
	t.Helper()
	if !slices.Equal(got, want) {
		t.Errorf("sites = %v\nwant    %v", got, want)
	}
}

func TestMethodRefsAcrossPackagesAndTests(t *testing.T) {
	root := writeProject(t, fixture)
	assertSites(t, refSites(t, indexed(t, root, "lib/lib.go", "Save")), []string{
		"lib/ext_test.go:19",
		"lib/lib.go:31",
		"lib/lib_test.go:7",
	})
}

func TestFieldAndConstRefs(t *testing.T) {
	root := writeProject(t, fixture)
	assertSites(t, refSites(t, byPath(root, "example.com/m/lib.Store.Name")), []string{
		"lib/lib.go:30",
		"lib/lib_test.go:8",
	})
	assertSites(t, refSites(t, indexed(t, root, "lib/lib.go", "Limit")), []string{
		"lib/lib_test.go:9",
	})
}

func TestRefsAcrossModules(t *testing.T) {
	root := writeProject(t, fixture)
	// Root module into a replaced nested module, and that module's tests.
	assertSites(t, refSites(t, indexed(t, root, "sub/sub.go", "Greet")), []string{
		"lib/lib.go:21",
		"sub/sub_test.go:5",
	})
	// A module loaded on its own back into the root module.
	assertSites(t, refSites(t, indexed(t, root, "lib/lib.go", "Exported")), []string{
		"lib/ext_test.go:18",
		"tool/main.go:6",
	})
	assertSites(t, refSites(t, byPath(root, "example.com/m/lib.Store")), []string{
		"lib/ext_test.go:16",
		"lib/ext_test.go:19",
		"lib/lib.go:11",
		"lib/lib.go:29",
		"lib/lib.go:30",
		"lib/lib_test.go:6",
		"tool/main.go:7",
	})
}

func implementations(t *testing.T, params rpc.SymbolTargetParams) map[string]bool {
	t.Helper()
	res, err := Implementers(params)
	if err != nil {
		t.Fatal(err)
	}
	out := map[string]bool{}
	for _, impl := range slices.Concat(res.Implementers, res.Interfaces) {
		out[impl.QualifiedPath] = impl.Pointer
	}
	return out
}

func assertImplementations(t *testing.T, got, want map[string]bool) {
	t.Helper()
	if len(got) != len(want) {
		t.Errorf("got %v, want %v", got, want)
	}
	for qpath, pointer := range want {
		if gotPointer, ok := got[qpath]; !ok || gotPointer != pointer {
			t.Errorf("%s: got (present=%v pointer=%v), want pointer=%v", qpath, ok, gotPointer, pointer)
		}
	}
}

func TestInterfaceImplementers(t *testing.T) {
	root := writeProject(t, fixture)
	assertImplementations(t, implementations(t, indexed(t, root, "lib/lib.go", "Saver")), map[string]bool{
		"example.com/m/lib.Store":         true,
		"example.com/m/lib.Value":         false,
		"example.com/m/lib_test.extSaver": true,
	})
}

func TestDependencyInterfaceImplementers(t *testing.T) {
	root := writeProject(t, fixture)
	assertImplementations(t, implementations(t, byPath(root, "fmt.Stringer")), map[string]bool{
		"example.com/m/lib.Named": false,
	})
}

func TestInterfacesOfConcreteType(t *testing.T) {
	root := writeProject(t, fixture)
	assertImplementations(t, implementations(t, indexed(t, root, "lib/lib.go", "Store")), map[string]bool{
		"example.com/m/lib.Saver":      true,
		"example.com/m/lib_test.saver": true,
	})
	got := implementations(t, indexed(t, root, "lib/lib.go", "Named"))
	if pointer, ok := got["fmt.Stringer"]; !ok || pointer {
		t.Errorf("Named should implement fmt.Stringer by value, got %v", got)
	}
}

func TestImplementersRejectsNonType(t *testing.T) {
	root := writeProject(t, fixture)
	_, err := Implementers(indexed(t, root, "lib/lib.go", "Exported"))
	var qerr *QueryError
	if !errors.As(err, &qerr) || qerr.Code != "invalid_target" {
		t.Errorf("err = %v, want invalid_target", err)
	}
}

func TestUnknownQualifiedPath(t *testing.T) {
	root := writeProject(t, fixture)
	_, err := References(byPath(root, "example.com/m/lib.Missing"))
	var qerr *QueryError
	if !errors.As(err, &qerr) || qerr.Code != "symbol_not_found" {
		t.Errorf("err = %v, want symbol_not_found", err)
	}
}

func TestTypeErrorsFailTheQuery(t *testing.T) {
	files := maps(fixture, map[string]string{
		"lib/broken.go": "package lib\n\nfunc broken() int { return missing }\n",
	})
	root := writeProject(t, files)
	_, err := References(indexed(t, root, "lib/lib.go", "Exported"))
	var lerr *project.LoadError
	if !errors.As(err, &lerr) || lerr.Code != "load_failed" || len(lerr.Errors) == 0 {
		t.Fatalf("err = %v, want load_failed with the loader's messages", err)
	}
}

func maps(base, extra map[string]string) map[string]string {
	out := map[string]string{}
	for k, v := range base {
		out[k] = v
	}
	for k, v := range extra {
		out[k] = v
	}
	return out
}
