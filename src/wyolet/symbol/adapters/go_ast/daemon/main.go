// Command go-scan is the Go-language worker for the symbol adapter
// JSON-RPC protocol. Spawned by the Python GoAstAdapter, reads requests
// from stdin, writes responses to stdout. One worker per orchestrator.
package main

import (
	"encoding/json"
	"fmt"
	"go/parser"
	"go/scanner"
	"go/token"
	"os"

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
