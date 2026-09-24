use std::process::Command;

#[test]
fn version_flag_prints_crate_version() {
    let output = Command::new(env!("CARGO_BIN_EXE_coban-detect"))
        .arg("--version")
        .output()
        .expect("coban-detect binary should run");

    assert!(output.status.success(), "exit status: {}", output.status);
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        format!("coban-detect {}\n", env!("CARGO_PKG_VERSION"))
    );
}
