#[test]
fn usage_lists_supported_commands_and_rejects_stock_zellij() -> std::io::Result<()> {
    let output = std::process::Command::new(env!("CARGO_BIN_EXE_tdnext"))
        .arg("install-zellij")
        .output()?;
    assert_eq!(output.status.code(), Some(2));
    let text = String::from_utf8_lossy(&output.stderr);
    assert!(
        text.starts_with("tdnext: unknown command 'install-zellij'"),
        "{text}"
    );
    let usage = text.split_once("commands:").unwrap_or_default().1;
    for command in [
        "list",
        "install",
        "enable",
        "disable",
        "uninstall",
        "desktop",
        "run",
    ] {
        assert!(
            usage
                .lines()
                .any(|line| line.split_whitespace().next() == Some(command)),
            "usage must document {command}"
        );
    }
    assert!(!usage.contains("install-zellij"));
    Ok(())
}
