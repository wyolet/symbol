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
