use std::process::Command;

#[test]
fn version_flag_prints_crate_version() {
    let output = Command::new(env!("CARGO_BIN_EXE_baton-detect"))
        .arg("--version")
        .output()
        .expect("baton-detect binary should run");

    assert!(output.status.success(), "exit status: {}", output.status);
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        format!("baton-detect {}\n", env!("CARGO_PKG_VERSION"))
    );
}

// Helpers of a test file: a failure should stop the test, with the error.
#[allow(clippy::unwrap_used)]
mod usage {
    use std::io::{BufRead, BufReader, Write};
    use std::path::{Path, PathBuf};
    use std::process::{Command, Stdio};
    use std::sync::mpsc;
    use std::time::Duration;

    use serde_json::Value;

    const DATA: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/data");

    /// A home-like tree with the synthetic logs where the agents would put them.
    fn sources(name: &str) -> PathBuf {
        let root =
            std::env::temp_dir().join(format!("baton-detect-cli-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let claude = root.join("claude/projects/-w");
        let codex = root.join("codex/sessions/2026/10/01");
        std::fs::create_dir_all(&claude).unwrap();
        std::fs::create_dir_all(&codex).unwrap();
        std::fs::copy(
            format!("{DATA}/claude-split.jsonl"),
            claude.join("s-1.jsonl"),
        )
        .unwrap();
        std::fs::copy(
            format!("{DATA}/codex-resume.jsonl"),
            codex.join("rollout-2026-10-01T10-00-00-c-1.jsonl"),
        )
        .unwrap();
        root
    }

    fn command(root: &Path) -> Command {
        let mut command = Command::new(env!("CARGO_BIN_EXE_baton-detect"));
        command
            .args(["usage", "--claude-dir"])
            .arg(root.join("claude/projects"));
        command.arg("--codex-dir").arg(root.join("codex/sessions"));
        command.arg("--opencode-db").arg(root.join("none.db"));
        command
    }

    fn lines(output: &[u8]) -> Vec<Value> {
        String::from_utf8_lossy(output)
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect()
    }

    #[test]
    fn once_reads_every_source_and_exits() {
        let root = sources("once");
        let output = command(&root).arg("--once").output().unwrap();
        assert!(output.status.success());
        let events = lines(&output.stdout);
        let kinds: Vec<(&str, &str)> = events
            .iter()
            .map(|e| (e["kind"].as_str().unwrap(), e["agent"].as_str().unwrap()))
            .collect();
        // 3 Claude responses, then Codex: 5 usage records and 2 rate-limit changes.
        assert_eq!(kinds.iter().filter(|k| k.1 == "claude").count(), 3);
        assert_eq!(
            kinds.iter().filter(|k| **k == ("usage", "codex")).count(),
            5
        );
        assert_eq!(kinds.iter().filter(|k| k.0 == "rate_limits").count(), 2);
        // The broken Claude line and the missing database are reported, not fatal.
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(stderr.contains("s-1.jsonl"), "{stderr}");
        assert!(stderr.contains("none.db not found"), "{stderr}");
    }

    #[test]
    fn since_leaves_out_earlier_events() {
        let root = sources("since");
        let output = command(&root)
            .args(["--once", "--since", "2026-10-01T10:05:00Z"])
            .output()
            .unwrap();
        let events = lines(&output.stdout);
        assert!(
            events
                .iter()
                .all(|e| e["at"].as_str().unwrap() >= "2026-10-01T10:05:00")
        );
        assert_eq!(events.len(), 3, "{events:?}"); // the three Codex counts after the resume
    }

    #[test]
    fn follow_reports_lines_as_they_are_written() {
        let root = sources("follow");
        let mut child = command(&root)
            .args(["--poll-ms", "50"])
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let stdout = child.stdout.take().unwrap();
        let (sender, received) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if sender.send(line).is_err() {
                    break;
                }
            }
        });
        let wait = Duration::from_secs(10);
        for _ in 0..10 {
            received.recv_timeout(wait).expect("the initial scan");
        }
        let transcript = root.join("claude/projects/-w/s-1.jsonl");
        let mut file = std::fs::OpenOptions::new()
            .append(true)
            .open(transcript)
            .unwrap();
        writeln!(
            file,
            r#"{{"type":"assistant","sessionId":"s-1","timestamp":"2026-10-01T11:00:00Z","message":{{"id":"msg_new","model":"m","usage":{{"input_tokens":1,"output_tokens":2}}}}}}"#
        )
        .unwrap();
        let line = received.recv_timeout(wait).expect("the appended response");
        child.kill().unwrap();
        child.wait().unwrap();
        let event: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(event["record_id"], "claude:msg_new");
    }
}
