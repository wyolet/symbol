//! Newline-delimited JSON-RPC 2.0 client over a child process's stdio —
//! the transport adapters/go_ast/__init__.py speaks to daemon/go-scan,
//! reused for pyscan. One in-flight request at a time (line framing).

use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::Mutex;

use anyhow::{anyhow, bail, Context, Result};

pub struct Daemon {
    child: Child,
    io: Mutex<(ChildStdin, BufReader<ChildStdout>, u64)>,
    pub name: String,
}

impl Daemon {
    pub fn spawn(name: &str, argv: &[String], init_params: serde_json::Value) -> Result<Daemon> {
        let mut cmd = Command::new(&argv[0]);
        cmd.args(&argv[1..])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        let mut child = cmd
            .spawn()
            .with_context(|| format!("spawning {name} daemon: {argv:?}"))?;
        let stdin = child.stdin.take().ok_or_else(|| anyhow!("no stdin"))?;
        let stdout = BufReader::new(child.stdout.take().ok_or_else(|| anyhow!("no stdout"))?);
        let daemon = Daemon {
            child,
            io: Mutex::new((stdin, stdout, 0)),
            name: name.to_owned(),
        };
        daemon
            .call("initialize", init_params)
            .with_context(|| format!("{name} initialize handshake"))?;
        Ok(daemon)
    }

    pub fn call(&self, method: &str, params: serde_json::Value) -> Result<serde_json::Value> {
        self.call_serving(method, params, &|m, _| {
            bail!("unexpected daemon request {m:?}")
        })
    }

    /// `call`, answering any requests the daemon sends back before its
    /// response (pyscan's `index/*` queries during rename analysis).
    pub fn call_serving(
        &self,
        method: &str,
        params: serde_json::Value,
        serve: &dyn Fn(&str, &serde_json::Value) -> Result<serde_json::Value>,
    ) -> Result<serde_json::Value> {
        let mut io = self.io.lock().unwrap();
        io.2 += 1;
        let req = serde_json::json!({
            "jsonrpc": "2.0", "id": io.2, "method": method, "params": params,
        });
        let line = serde_json::to_string(&req)?;
        io.0.write_all(line.as_bytes())?;
        io.0.write_all(b"\n")?;
        io.0.flush()?;

        loop {
            let mut resp_line = String::new();
            let n = io.1.read_line(&mut resp_line)?;
            if n == 0 {
                bail!("{} daemon exited mid-request ({method})", self.name);
            }
            let resp: serde_json::Value = serde_json::from_str(resp_line.trim())
                .with_context(|| format!("{} daemon sent invalid JSON", self.name))?;
            if let Some(query) = resp.get("method").and_then(|m| m.as_str()) {
                let null = serde_json::Value::Null;
                let reply = match serve(query, resp.get("params").unwrap_or(&null)) {
                    Ok(result) => serde_json::json!({
                        "jsonrpc": "2.0", "id": resp["id"], "result": result,
                    }),
                    Err(e) => serde_json::json!({
                        "jsonrpc": "2.0", "id": resp["id"],
                        "error": {"code": -32603, "message": e.to_string()},
                    }),
                };
                io.0.write_all(serde_json::to_string(&reply)?.as_bytes())?;
                io.0.write_all(b"\n")?;
                io.0.flush()?;
                continue;
            }
            if let Some(err) = resp.get("error") {
                bail!("{} {method}: {err}", self.name);
            }
            return resp
                .get("result")
                .cloned()
                .ok_or_else(|| anyhow!("{} {method}: response has no result", self.name));
        }
    }
}

impl Drop for Daemon {
    fn drop(&mut self) {
        // go_ast._stop: shutdown notification, brief wait, then kill.
        if let Ok(mut io) = self.io.lock() {
            let _ =
                io.0.write_all(b"{\"jsonrpc\":\"2.0\",\"method\":\"shutdown\"}\n");
            let _ = io.0.flush();
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}
