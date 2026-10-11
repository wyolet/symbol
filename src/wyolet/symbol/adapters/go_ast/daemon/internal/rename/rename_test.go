package rename

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/wyolet/symbol/go-scan/internal/rpc"
	"github.com/wyolet/symbol/go-scan/internal/scan"
)

const modulePath = "example.com/m"

var testFixture = map[string]string{
	"go.mod": "module example.com/m\n\ngo 1.22\n",
	"lib/lib.go": `package lib

func helper(s string) string { return s }

func Exported() string { return helper("x") }

type Store struct{}

func (s *Store) Save() error { return nil }

type Record struct{ Save bool }
`,
	"lib/lib_test.go": `package lib

import "testing"

func fixture() string { return helper("y") }

func TestHelper(t *testing.T) {
	_ = fixture()
	_ = helper("z")
	_ = (&Store{}).Save()
}

func savedRecord() bool { return Record{}.Save }
`,
	"lib/ext_test.go": `package lib_test

import (
	"testing"

	"example.com/m/lib"
)

type saver interface{ Save() error }

func extFixture() *lib.Store { return &lib.Store{} }

func TestExported(t *testing.T) {
	_ = lib.Exported()
	var s saver = extFixture()
	_ = s
	_ = extFixture().Save()
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

// declRange returns the byte range the index records for `name` in rel,
// using the same scanner the index is built from.
func declRange(t *testing.T, root, rel, name string) [2]int {
	t.Helper()
	src, err := os.ReadFile(filepath.Join(root, rel))
	if err != nil {
		t.Fatal(err)
	}
	for _, sym := range scan.File(rel, string(src), modulePath+"/lib").Symbols {
		if sym.Name == name {
			return sym.ByteRange
		}
	}
	t.Fatalf("%s not declared in %s", name, rel)
	return [2]int{}
}

func renameBinding(t *testing.T, root, declaringFile, leaf string) rpc.RenameResult {
	t.Helper()
	res, err := ModuleBinding(rpc.RenameModuleBindingParams{
		ProjectRoot:       root,
		Leaf:              leaf,
		NewName:           leaf + "Renamed",
		TargetQpath:       modulePath + "/lib." + leaf,
		TargetModuleQpath: modulePath + "/lib",
		DeclaringFile:     declaringFile,
		DeclByteRange:     declRange(t, root, declaringFile, leaf),
	})
	if err != nil {
		t.Fatal(err)
	}
	return res
}

// rewrittenLines maps each file to the 1-based lines rewritten in it,
// failing on any byte range reported twice.
func rewrittenLines(t *testing.T, res rpc.RenameResult) map[string][]int {
	t.Helper()
	out := map[string][]int{}
	for rel, a := range res.Files {
		if strings.HasPrefix(rel, "..") || filepath.IsAbs(rel) {
			t.Errorf("file outside project in result: %s", rel)
		}
		seen := map[[2]int]bool{}
		for _, r := range a.Rewrites {
			span := [2]int{r.ByteStart, r.ByteEnd}
			if seen[span] {
				t.Errorf("%s: duplicate rewrite at %v", rel, span)
			}
			seen[span] = true
			out[rel] = append(out[rel], r.Line)
		}
		if len(a.Unresolved) > 0 {
			t.Errorf("%s: unexpected unresolved sites %+v", rel, a.Unresolved)
		}
	}
	return out
}

func assertLines(t *testing.T, got, want map[string][]int) {
	t.Helper()
	for rel := range got {
		if _, ok := want[rel]; !ok {
			t.Errorf("unexpected rewrites in %s: lines %v", rel, got[rel])
		}
	}
	for rel, lines := range want {
		if !slices.Equal(got[rel], lines) {
			t.Errorf("%s: rewritten lines %v, want %v", rel, got[rel], lines)
		}
	}
}

func TestModuleBindingCoversInPackageTests(t *testing.T) {
	root := writeProject(t, testFixture)
	got := rewrittenLines(t, renameBinding(t, root, "lib/lib.go", "helper"))
	assertLines(t, got, map[string][]int{
		"lib/lib.go":      {3, 5},
		"lib/lib_test.go": {5, 9},
	})
}

func TestModuleBindingCoversExternalTestPackage(t *testing.T) {
	root := writeProject(t, testFixture)
	got := rewrittenLines(t, renameBinding(t, root, "lib/lib.go", "Exported"))
	assertLines(t, got, map[string][]int{
		"lib/lib.go":      {5},
		"lib/ext_test.go": {14},
	})
}

func TestTestOnlySymbolInPackageTest(t *testing.T) {
	root := writeProject(t, testFixture)
	got := rewrittenLines(t, renameBinding(t, root, "lib/lib_test.go", "fixture"))
	assertLines(t, got, map[string][]int{
		"lib/lib_test.go": {5, 8},
	})
}

func TestTestOnlySymbolInExternalTestPackage(t *testing.T) {
	root := writeProject(t, testFixture)
	got := rewrittenLines(t, renameBinding(t, root, "lib/ext_test.go", "extFixture"))
	assertLines(t, got, map[string][]int{
		"lib/ext_test.go": {11, 15, 17},
	})
}

func TestMemberCoversAllTestVariants(t *testing.T) {
	root := writeProject(t, testFixture)
	res, err := Member(rpc.RenameMemberParams{
		ProjectRoot:      root,
		Leaf:             "Save",
		NewName:          "Persist",
		TargetQpath:      modulePath + "/lib.Store.Save",
		TargetOwnerQpath: modulePath + "/lib.Store",
		DeclaringFile:    "lib/lib.go",
		DeclByteRange:    declRange(t, root, "lib/lib.go", "Save"),
	})
	if err != nil {
		t.Fatal(err)
	}
	assertLines(t, rewrittenLines(t, res), map[string][]int{
		"lib/lib.go":      {9},
		"lib/lib_test.go": {10},
		"lib/ext_test.go": {17},
	})

	skipped := res.Files["lib/lib_test.go"].SkippedMismatch
	if len(skipped) != 1 || skipped[0].Line != 13 || skipped[0].ResolvedToQpath != modulePath+"/lib.Record.Save" {
		t.Errorf("skipped = %+v, want the Record.Save field selection", skipped)
	}

	affected := res.Files["lib/lib.go"].AffectedInterfaces
	if len(affected) != 1 || affected[0].InterfaceQpath != modulePath+"/lib_test.saver" || affected[0].File != "lib/ext_test.go" {
		t.Errorf("affected interfaces = %+v, want the test-only saver", affected)
	}
}

func TestModuleBindingCoversNestedModule(t *testing.T) {
	files := map[string]string{
		"tool/go.mod": "module example.com/tool\n\ngo 1.22\n\nrequire example.com/m v0.0.0\n\nreplace example.com/m => ../\n",
		"tool/main.go": `package main

import "example.com/m/lib"

func main() { _ = lib.Exported() }
`,
	}
	for rel, src := range testFixture {
		files[rel] = src
	}
	root := writeProject(t, files)
	got := rewrittenLines(t, renameBinding(t, root, "lib/lib.go", "Exported"))
	assertLines(t, got, map[string][]int{
		"lib/lib.go":      {5},
		"lib/ext_test.go": {14},
		"tool/main.go":    {5},
	})
}

func TestUnresolvableReferenceIsReported(t *testing.T) {
	files := map[string]string{
		"go.mod":     testFixture["go.mod"],
		"lib/lib.go": testFixture["lib/lib.go"],
		"lib/broken_test.go": `package lib

func useHelper() string { return missing.helper("x") + helper("y") }
`,
	}
	root := writeProject(t, files)
	res := renameBinding(t, root, "lib/lib.go", "helper")

	broken := res.Files["lib/broken_test.go"]
	if len(broken.Unresolved) != 1 || broken.Unresolved[0].Line != 3 {
		t.Errorf("unresolved = %+v, want the selector on line 3", broken.Unresolved)
	}
	if len(broken.Rewrites) != 1 || broken.Rewrites[0].Line != 3 {
		t.Errorf("rewrites = %+v, want the resolvable call on line 3", broken.Rewrites)
	}
}
