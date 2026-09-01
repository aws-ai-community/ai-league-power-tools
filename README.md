# AI League Power Tools

Tools to make participating in [AWS AI League](https://aileague.aws.dev) easier. Automate the tedious parts so you can focus on prompt engineering, fine-tuning, and strategy.

## What is AWS AI League?

AWS AI League is a competitive game where you build an AI agent to navigate a dungeon map, solve challenges (math, web search, code execution, guardrails, key/door puzzles), and maximise your score. Your agent is composed of:

- **Supervisor** - the main LLM that receives challenges and decides how to respond
- **Lambda Tools** - serverless functions the supervisor can call (pathfinding, web fetch, code execution, etc.)
- **Sub-Agents** - secondary LLMs the supervisor can delegate tasks to (useful for fine-tuned models)
- **Memory** - persistent key-value storage across challenge turns (stores keys for door puzzles)
- **Guardrail** - content filters that block harmful/off-topic inputs for guardrail challenges

## Tools

| Tool | Description |
|------|-------------|
| [deploy-agents](./deploy-agents/) | Deploy your full agent configuration (supervisor, tools, sub-agents, memory, guardrail) via the website's GraphQL API. No clickops. |

## Contributing

PRs welcome! If you've built tooling that helps with AI League participation, feel free to contribute.

## License

See [LICENSE](./LICENSE).
