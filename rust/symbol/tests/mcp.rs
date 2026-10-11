//! `symbol mcp` over real stdio: protocol negotiation, tool listing, tool
//! results and tool errors, and the session read cache that gates Patch.

use std::io::{BufRead, BufReader, Write};
use std::path::Path;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

use serde_json::{json, Value};

struct Server {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    next_id: i64,
}

impl Server {
    fn start(root: &Path) -> Server {
        let mut child = Command::new(env!("CARGO_BIN_EXE_symbol"))
            .env("UV_CACHE_DIR", root.join(".uv-cache"))
            .env("SYMBOL_MCP_WATCHDOG", "0")
            .env("SYMBOL_MCP_NICE", "0")
            .current_dir(root)
            .arg("--root")
            .arg(root)
            .arg("mcp")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn symbol mcp");
        let stdin = child.stdin.take().unwrap();
        let stdout = BufReader::new(child.stdout.take().unwrap());
        Server {
            child,
            stdin,
            stdout,
            next_id: 0,
        }
    }

    fn send(&mut self, message: &Value) {
        self.send_raw(&message.to_string());
    }

    fn send_raw(&mut self, line: &str) {
        self.stdin.write_all(line.as_bytes()).unwrap();
        self.stdin.write_all(b"\n").unwrap();
        self.stdin.flush().unwrap();
    }

    fn read(&mut self) -> Value {
        let mut line = String::new();
        self.stdout.read_line(&mut line).unwrap();
        serde_json::from_str(&line).unwrap_or_else(|e| panic!("bad reply {line:?}: {e}"))
    }

    fn request(&mut self, method: &str, params: Value) -> Value {
        self.next_id += 1;
        let id = self.next_id;
        self.send(&json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}));
        let reply = self.read();
        assert_eq!(reply["id"], id, "{reply}");
        reply
    }

    fn call(&mut self, tool: &str, arguments: Value) -> Value {
        let reply = self.request("tools/call", json!({"name": tool, "arguments": arguments}));
        reply["result"].clone()
    }

    fn text(&mut self, tool: &str, arguments: Value) -> String {
        let result = self.call(tool, arguments);
        assert_eq!(result["isError"], false, "{result}");
        result["content"][0]["text"].as_str().unwrap().to_owned()
    }

    fn json(&mut self, tool: &str, arguments: Value) -> Value {
        serde_json::from_str(&self.text(tool, arguments)).unwrap()
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn empty_project() -> tempfile::TempDir {
    tempfile::tempdir().unwrap()
}

#[test]
fn initialize_negotiates_protocol_version() {
    let dir = empty_project();
    let mut server = Server::start(&dir.path().canonicalize().unwrap());
    let known = server.request("initialize", json!({"protocolVersion": "2025-06-18"}));
    assert_eq!(known["result"]["protocolVersion"], "2025-06-18");
    assert_eq!(known["result"]["serverInfo"]["name"], "symbol");
    assert_eq!(
        known["result"]["capabilities"]["tools"],
        json!({"listChanged": false})
    );
    let unknown = server.request("initialize", json!({"protocolVersion": "1999-01-01"}));
    assert_eq!(unknown["result"]["protocolVersion"], "2025-11-25");

    // Notifications get no reply: the next line read answers the ping.
    server.send(&json!({"jsonrpc": "2.0", "method": "notifications/initialized"}));
    let ping = server.request("ping", json!({}));
    assert_eq!(ping["result"], json!({}));
}

#[test]
fn tools_list_serves_shared_descriptions() {
    let dir = empty_project();
    let mut server = Server::start(&dir.path().canonicalize().unwrap());
    let reply = server.request("tools/list", json!({}));
    let tools = reply["result"]["tools"].as_array().unwrap();
    let names: Vec<&str> = tools.iter().map(|t| t["name"].as_str().unwrap()).collect();
    assert_eq!(
        names,
        [
            "SearchSymbol",
            "SymbolBody",
            "SymbolOutline",
            "SymbolCallers",
            "Patch",
            "MultiPatch",
            "DeleteSymbol",
            "InsertSymbol",
            "RenameSymbol",
            "Refresh",
            "Undo",
            "ReplaceSymbol",
        ]
    );
    let source = std::fs::read_to_string(
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src/wyolet/symbol/data/mcp_tools.toml"),
    )
    .unwrap();
    let table: toml::Table = source.parse().unwrap();
    for tool in tools {
        let name = tool["name"].as_str().unwrap();
        assert_eq!(
            tool["description"].as_str(),
            table[name]["description"].as_str(),
            "{name}"
        );
        // Text-returning tools declare the {"result": str} output schema.
        let text_tool = ["Patch", "MultiPatch", "DeleteSymbol", "InsertSymbol"]
            .iter()
            .chain(&["RenameSymbol", "ReplaceSymbol"])
            .any(|t| *t == name);
        assert_eq!(tool.get("outputSchema").is_some(), text_tool, "{name}");
    }
}

#[test]
fn tool_results_and_errors_match_fastmcp_shapes() {
    let dir = empty_project();
    let mut server = Server::start(&dir.path().canonicalize().unwrap());

    let search = server.call("SearchSymbol", json!({"patterns": ["x"]}));
    assert_eq!(search["isError"], false);
    assert!(search.get("structuredContent").is_none());
    let payload: Value =
        serde_json::from_str(search["content"][0]["text"].as_str().unwrap()).unwrap();
    assert_eq!(
        payload,
        json!({"ok": true, "count": 0, "hits": [], "index_status": "empty"})
    );

    let missing = server.call("SearchSymbol", json!({}));
    assert_eq!(missing["isError"], true);
    let text = missing["content"][0]["text"].as_str().unwrap();
    assert!(
        text.starts_with("Error executing tool SearchSymbol: 1 validation error"),
        "{text}"
    );

    let unknown = server.call("Nope", json!({}));
    assert_eq!(unknown["isError"], true);
    assert_eq!(unknown["content"][0]["text"], "Unknown tool: Nope");

    let refused = server.call("Patch", json!({"file": "absent.py", "range": "1-1"}));
    assert_eq!(refused["isError"], false);
    let expected = "status: error\nerror_code: not_found\nmessage: file not found: absent.py";
    assert_eq!(refused["content"][0]["text"], expected);
    assert_eq!(refused["structuredContent"]["result"], expected);

    let method = server.request("bogus/method", json!({}));
    assert_eq!(method["error"]["code"], -32601);
    server.send_raw("{not json");
    assert_eq!(server.read()["error"]["code"], -32700);

    let undo = server.json("Undo", json!({}));
    assert_eq!(undo["status"], "nothing_to_undo");
    assert_eq!(undo["ok"], true);
}

#[test]
fn patch_requires_a_read_then_passes_on_retry() {
    let dir = empty_project();
    let root = dir.path().canonicalize().unwrap();
    std::fs::write(root.join("mod.py"), "def f():\n    return 1\n").unwrap();
    let mut server = Server::start(&root);

    let patch = json!({"file": "mod.py", "range": "2-2", "content": "    return 2"});
    let first = server.text("Patch", patch.clone());
    assert!(
        first.starts_with("status: needs_read_confirmation\nfile: mod.py\n"),
        "{first}"
    );
    let second = server.text("Patch", patch);
    assert!(
        second.starts_with("status: applied\nfile: mod.py\n"),
        "{second}"
    );
    assert_eq!(
        std::fs::read_to_string(root.join("mod.py")).unwrap(),
        "def f():\n    return 2\n"
    );

    // A SymbolBody read covers the symbol's lines for the next edit, and the
    // index already reflects the previous write.
    let body = server.json("SymbolBody", json!({"target": "mod.f"}));
    assert_eq!(body["body"], "def f():\n    return 2");
    assert_eq!(body["total_lines"], 2);
    let multi = server.text(
        "MultiPatch",
        json!({"file": "mod.py", "edits": [{"range": "1-1", "content": "def g():"}]}),
    );
    assert!(
        multi.starts_with("status: applied\nfile: mod.py\n"),
        "{multi}"
    );
    let hits = server.json("SearchSymbol", json!({"patterns": ["g"]}));
    assert_eq!(hits["count"], 1, "{hits}");
}
