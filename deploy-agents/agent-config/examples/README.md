# Agent Configuration Examples

These are reference configurations you can copy into the live `agent-config/` folder. Files in this `examples/` directory are **never deployed or processed** by the system.

## Available Configs

| Folder | Description |
|--------|-------------|
| `default/` | Simple setup: supervisor + Pathfinding Specialist + Pathfinder tool. No memory, no guardrail. |
| `full-config/` | All features: 2 sub-agents (Pathfinder + Code Calculator), 2 tools, memory, guardrail with content filters and deny topic. |
