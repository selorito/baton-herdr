# fake-agent

A scripted stand-in for a coding agent, for demos and live tests; it spends no quota. It
prints screens shaped like the real agent's. Run under the agent's name inside a herdr pane,
it is recognised by herdr as that agent, herdr derives its state from the screens with its own
rules, and it reports a session id the way the agent's herdr hook does.

```bash
uv run tools/fake-agent/fake_agent.py --make-launcher /tmp/fakebin claude
/tmp/fakebin/claude tools/fake-agent/scripts/claude-limit.toml
```

It reports its session under the official integration source name (`herdr:claude`), so use it
only in throwaway herdr sessions, never in one where the real agent runs. Scripts live in
`scripts/`; see the docstring of `fake_agent.py` for the format. Screens must stay free of
real paths, names and keys: `just fixtures-audit` does not scan this directory.
