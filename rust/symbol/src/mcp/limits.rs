//! Resource bounds for the long-lived server, configured by the same env
//! vars as the Python server: SYMBOL_MCP_NICE, SYMBOL_MCP_MEM_LIMIT_MB,
//! SYMBOL_MCP_MEM_HARD, SYMBOL_MCP_WATCHDOG, SYMBOL_MCP_WATCHDOG_INTERVAL.
//! All of it is POSIX-only and compiles to no-ops elsewhere. Language
//! daemons are separate processes, so the RSS ceiling covers this process
//! only.

/// Soft RSS ceiling in MB (`SYMBOL_MCP_MEM_LIMIT_MB`, default 2048, 0 = off).
pub fn mem_limit_mb() -> i64 {
    std::env::var("SYMBOL_MCP_MEM_LIMIT_MB")
        .ok()
        .and_then(|raw| raw.trim().parse().ok())
        .unwrap_or(2048)
}

/// Lower our scheduling priority (`SYMBOL_MCP_NICE`, default 10, <= 0 off)
/// so a cold-start index build doesn't starve the user's foreground work.
/// An unprivileged process can't raise it back, so the whole server runs
/// niced; tool calls are light enough that this costs little.
pub fn apply_nice() {
    let Ok(increment) = std::env::var("SYMBOL_MCP_NICE")
        .unwrap_or_else(|_| "10".into())
        .trim()
        .parse::<i32>()
    else {
        return;
    };
    if increment <= 0 {
        return;
    }
    #[cfg(unix)]
    // SAFETY: nice(2) only adjusts this process's priority.
    unsafe {
        libc::nice(increment);
    }
    #[cfg(not(unix))]
    let _ = increment;
}

/// Opt-in kernel cap on address space (`SYMBOL_MCP_MEM_HARD=1`) at the soft
/// ceiling. RLIMIT_AS counts virtual memory, so it is off by default; macOS
/// does not enforce it, leaving the watchdog as the guard there.
pub fn apply_hard_mem_limit() {
    if std::env::var("SYMBOL_MCP_MEM_HARD").as_deref() != Ok("1") {
        return;
    }
    let limit_mb = mem_limit_mb();
    if limit_mb <= 0 {
        return;
    }
    #[cfg(unix)]
    // SAFETY: get/setrlimit read and write a stack-allocated struct.
    unsafe {
        let cap = (limit_mb as libc::rlim_t).saturating_mul(1024 * 1024);
        let mut current: libc::rlimit = std::mem::zeroed();
        if libc::getrlimit(libc::RLIMIT_AS, &mut current) != 0 {
            return;
        }
        let hard = if current.rlim_max != libc::RLIM_INFINITY && current.rlim_max < cap {
            current.rlim_max
        } else {
            cap
        };
        let wanted = libc::rlimit {
            rlim_cur: cap.min(hard),
            rlim_max: hard,
        };
        libc::setrlimit(libc::RLIMIT_AS, &wanted);
    }
    #[cfg(not(unix))]
    let _ = limit_mb;
}

/// Peak resident memory of this process in bytes (`ru_maxrss` is bytes on
/// macOS, kilobytes elsewhere).
#[cfg(unix)]
pub fn peak_rss_bytes() -> u64 {
    // SAFETY: getrusage fills a stack-allocated struct.
    let peak = unsafe {
        let mut usage: libc::rusage = std::mem::zeroed();
        if libc::getrusage(libc::RUSAGE_SELF, &mut usage) != 0 {
            return 0;
        }
        usage.ru_maxrss.max(0) as u64
    };
    if cfg!(target_os = "macos") {
        peak
    } else {
        peak * 1024
    }
}

/// Exit when the parent dies (we get reparented) or peak RSS crosses the
/// soft ceiling. A stdio server should die with its client; launchers like
/// `uv run` can exit without reaping us. `SYMBOL_MCP_WATCHDOG=0` disables.
pub fn install_watchdog() {
    if std::env::var("SYMBOL_MCP_WATCHDOG").as_deref() == Ok("0") {
        return;
    }
    #[cfg(unix)]
    {
        let interval = std::env::var("SYMBOL_MCP_WATCHDOG_INTERVAL")
            .ok()
            .and_then(|raw| raw.trim().parse::<f64>().ok())
            .filter(|secs| secs.is_finite() && *secs >= 0.0)
            .unwrap_or(2.0);
        // SAFETY: getppid has no preconditions.
        let start_ppid = unsafe { libc::getppid() };
        let mem_cap = mem_limit_mb().max(0) as u64 * 1024 * 1024;
        let spawned = std::thread::Builder::new()
            .name("symbol-mcp-watchdog".into())
            .spawn(move || loop {
                std::thread::sleep(std::time::Duration::from_secs_f64(interval));
                // SAFETY: as above.
                let ppid = unsafe { libc::getppid() };
                if ppid == 1 || (start_ppid != 1 && ppid != start_ppid) {
                    std::process::exit(0);
                }
                if mem_cap > 0 && peak_rss_bytes() > mem_cap {
                    eprintln!(
                        "[symbol-mcp] peak RSS exceeded {}MB ceiling — exiting",
                        mem_cap >> 20
                    );
                    std::process::exit(137);
                }
            });
        drop(spawned);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mem_limit_parses_env_with_default() {
        std::env::set_var("SYMBOL_MCP_MEM_LIMIT_MB", "512");
        assert_eq!(mem_limit_mb(), 512);
        std::env::set_var("SYMBOL_MCP_MEM_LIMIT_MB", "garbage");
        assert_eq!(mem_limit_mb(), 2048);
        std::env::remove_var("SYMBOL_MCP_MEM_LIMIT_MB");
        assert_eq!(mem_limit_mb(), 2048);
    }

    #[cfg(unix)]
    #[test]
    fn peak_rss_is_nonzero() {
        assert!(peak_rss_bytes() > 0);
    }
}
