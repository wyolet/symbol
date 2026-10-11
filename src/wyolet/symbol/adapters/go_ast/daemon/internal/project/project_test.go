package project

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestClassify(t *testing.T) {
	cases := []struct {
		msg, code, required string
	}{
		{"/p/a.go:1:1: package requires newer Go version go1.27 (application built with go1.25)", "goscan_outdated", "go1.27"},
		{"go: go.mod requires go >= 1.27 (running go 1.25.6; GOTOOLCHAIN=local)", "toolchain_mismatch", "go1.27"},
		{"go: download go1.27.0 for darwin/arm64: toolchain not available", "toolchain_mismatch", "go1.27.0"},
		{"/p/a.go:3:30: undefined: missing", "load_failed", ""},
	}
	for _, c := range cases {
		got := classify([]string{c.msg})
		if got.Code != c.code || got.RequiredGo != c.required {
			t.Errorf("%q: got (%s, %s), want (%s, %s)", c.msg, got.Code, got.RequiredGo, c.code, c.required)
		}
	}
}

func TestNotAnalyzedReasons(t *testing.T) {
	root := t.TempDir()
	files := map[string]string{
		"go.mod":                 "module example.com/m\n\ngo 1.22\n",
		"a.go":                   "package m\n",
		"it_test.go":             "//go:build integration\n\npackage m\n",
		"either.go":              "//go:build   (foo||bar)  &&!baz\n\npackage m\n",
		"legacy.go":              "// +build foo,bar\n// +build !baz\n\npackage m\n",
		"sys_plan9.go":           "package m\n",
		"sys_solaris_sparc64.go": "package m\n",
		"tagged_plan9.go":        "//go:build foo || bar\n\npackage m\n",
	}
	for rel, src := range files {
		if err := os.WriteFile(filepath.Join(root, rel), []byte(src), 0o644); err != nil {
			t.Fatal(err)
		}
	}

	p, err := Load(root)
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]string{}
	for _, f := range p.NotAnalyzed {
		got[f.File] = f.Constraint
	}
	want := map[string]string{
		"it_test.go":             "integration",
		"either.go":              "(foo || bar) && !baz",
		"legacy.go":              "foo && bar && !baz",
		"sys_plan9.go":           "plan9",
		"sys_solaris_sparc64.go": "solaris && sparc64",
		"tagged_plan9.go":        "plan9 && (foo || bar)",
	}
	for file, reason := range want {
		if got[file] != reason {
			t.Errorf("%s: constraint %q, want %q", file, got[file], reason)
		}
	}
	if reason := exclusionReason(filepath.Join(root, "a.go")); reason != notInLoadedPackage {
		t.Errorf("unconstrained file: %q, want %q", reason, notInLoadedPackage)
	}
}

func TestLoadReusesUntilSourcesChange(t *testing.T) {
	root := t.TempDir()
	write := func(rel, src string) {
		if err := os.WriteFile(filepath.Join(root, rel), []byte(src), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write("go.mod", "module example.com/m\n\ngo 1.22\n")
	write("a.go", "package m\n\nfunc A() {}\n")

	first, err := Load(root)
	if err != nil {
		t.Fatal(err)
	}
	if again, _ := Load(root); again != first {
		t.Error("unchanged project was loaded again")
	}

	write("b.go", "package m\n\nfunc B() { A() }\n")
	later := time.Now().Add(time.Second)
	if err := os.Chtimes(filepath.Join(root, "b.go"), later, later); err != nil {
		t.Fatal(err)
	}
	changed, err := Load(root)
	if err != nil {
		t.Fatal(err)
	}
	if changed == first {
		t.Fatal("added file did not trigger a reload")
	}
	if err := changed.Err(); err != nil {
		t.Errorf("unexpected load error: %v", err)
	}
}
