// Command go-scan is the Go-language worker for the symbol adapter
// JSON-RPC protocol. Spawned by the Python GoAstAdapter, reads requests
// from stdin, writes responses to stdout. One worker per orchestrator.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/printer"
	"go/scanner"
	"go/token"
	"os"
	"strings"

	"github.com/wyolet/symbol/go-scan/internal/rpc"
	"github.com/wyolet/symbol/go-scan/internal/scan"
)

// Bumped when the worker implementation changes in user-visible ways
// (new fields, behavior). Independent of the JSON-RPC protocol version.
const workerVersion = "0.1.0"

// Capabilities this worker advertises beyond the v1 minimum.
// Empty for now — scan_file + validate_syntax are required of every
// worker and aren't listed in capabilities.
var capabilities = []string{}

func main() {
	srv := rpc.NewServer(os.Stdin, os.Stdout)

	srv.Register("initialize", handleInitialize)
	srv.Register("validate_syntax", handleValidateSyntax)
	srv.Register("scan_file", handleScanFile)
	srv.Register("signature", handleSignature)
	srv.Register("shutdown", func(params json.RawMessage) (any, error) {
		srv.Stop()
		return nil, nil
	})

	if err := srv.Serve(); err != nil {
		fmt.Fprintln(os.Stderr, "go-scan:", err)
		os.Exit(1)
	}
}

// ── method handlers ───────────────────────────────────────────────

func handleInitialize(params json.RawMessage) (any, error) {
	var p rpc.InitializeParams
	if err := json.Unmarshal(params, &p); err != nil {
		return nil, &rpc.JSONRPCError{Code: rpc.ErrInvalidParams, Message: err.Error()}
	}
	if p.ProtocolVersion != "1" {
		return nil, &rpc.JSONRPCError{
			Code:    rpc.ErrInvalidParams,
			Message: "unsupported protocol_version: " + p.ProtocolVersion,
		}
	}
	return rpc.InitializeResult{
		Language:      "go",
		WorkerVersion: workerVersion,
		Capabilities:  capabilities,
	}, nil
}

func handleValidateSyntax(params json.RawMessage) (any, error) {
	var p rpc.ValidateSyntaxParams
	if err := json.Unmarshal(params, &p); err != nil {
		return nil, &rpc.JSONRPCError{Code: rpc.ErrInvalidParams, Message: err.Error()}
	}
	fset := token.NewFileSet()
	_, err := parser.ParseFile(fset, "input.go", p.Source, parser.SkipObjectResolution)
	if err == nil {
		return rpc.ParseResult{OK: true}, nil
	}
	msg := err.Error()
	// Best-effort line extraction from "filename:line:col: msg" form.
	line := extractLine(err, fset)
	return rpc.ParseResult{OK: false, ErrorLine: line, ErrorMessage: &msg}, nil
}

func handleScanFile(params json.RawMessage) (any, error) {
	var p rpc.ScanFileParams
	if err := json.Unmarshal(params, &p); err != nil {
		return nil, &rpc.JSONRPCError{Code: rpc.ErrInvalidParams, Message: err.Error()}
	}
	return scan.File(p.Path, p.Source, p.ModulePrefix), nil
}

func handleSignature(params json.RawMessage) (any, error) {
	var p rpc.SignatureParams
	if err := json.Unmarshal(params, &p); err != nil {
		return nil, &rpc.JSONRPCError{Code: rpc.ErrInvalidParams, Message: err.Error()}
	}
	return rpc.SignatureResult{Signature: signatureFromSource(p.Source)}, nil
}

// signatureFromSource parses ``source`` as a partial Go file (synthesizing
// a package declaration if missing) and returns the printer-formatted
// declaration of the first top-level symbol, with any function body
// stripped. Uses go/parser + go/printer — no string parsing.
func signatureFromSource(source string) string {
	f, fset := parseFlexible(source)
	if f == nil || len(f.Decls) == 0 {
		return ""
	}
	return printDeclSignature(f.Decls[0], fset)
}

func printDeclSignature(decl ast.Decl, fset *token.FileSet) string {
	switch d := decl.(type) {
	case *ast.FuncDecl:
		// Print the FuncDecl with the body stripped — gives the canonical
		// "func [recv] Name(params) results" form, no trailing brace.
		clone := *d
		clone.Body = nil
		clone.Doc = nil
		var buf bytes.Buffer
		if err := printer.Fprint(&buf, fset, &clone); err != nil {
			return ""
		}
		return collapseWhitespace(buf.String())
	case *ast.GenDecl:
		// type / const / var — print the declaration as-is (no body to strip).
		clone := *d
		clone.Doc = nil
		var buf bytes.Buffer
		if err := printer.Fprint(&buf, fset, &clone); err != nil {
			return ""
		}
		return collapseWhitespace(buf.String())
	}
	return ""
}

func collapseWhitespace(s string) string {
	return strings.Join(strings.Fields(s), " ")
}

// parseFlexible parses ``source`` as a Go file, transparently retrying
// with a synthetic ``package _stub`` prelude when the input is a bare
// snippet (which Go's parser otherwise rejects with "expected 'package'").
// The parser itself decides whether the source is well-formed — no
// string heuristics about what looks like a package declaration.
func parseFlexible(source string) (*ast.File, *token.FileSet) {
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, "input.go", source, parser.SkipObjectResolution)
	if err == nil {
		return f, fset
	}
	wrapped := "package _stub\n" + source
	fset = token.NewFileSet()
	f, err = parser.ParseFile(fset, "input.go", wrapped, parser.SkipObjectResolution)
	if err == nil {
		return f, fset
	}
	return nil, nil
}

// extractLine pulls the line number out of a go/parser error.
// parser returns scanner.ErrorList; we read the first entry's Position.
func extractLine(err error, _ *token.FileSet) *int {
	if list, ok := err.(scanner.ErrorList); ok && len(list) > 0 {
		line := list[0].Pos.Line
		return &line
	}
	if se, ok := err.(*scanner.Error); ok {
		line := se.Pos.Line
		return &line
	}
	return nil
}
