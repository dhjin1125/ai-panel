import unittest
import sys

from ai_panel.config import AgentConfig, ModelOption
from ai_panel.runner import _build_invocation
from ai_panel.runner import _command_with_model
from ai_panel.runner import run_agent
import asyncio


class RunnerTest(unittest.TestCase):
    def test_prompt_placeholder_becomes_argument(self):
        command, stdin = _build_invocation(["tool", "{prompt}"], "hello")

        self.assertEqual(command, ["tool", "hello"])
        self.assertIsNone(stdin)

    def test_without_placeholder_uses_stdin(self):
        command, stdin = _build_invocation(["tool", "-"], "hello")

        self.assertEqual(command, ["tool", "-"])
        self.assertEqual(stdin, "hello")

    def test_model_arg_appends_for_plain_stdin_command(self):
        agent = AgentConfig(
            id="claude",
            command=["claude", "--print"],
            model_arg=["--model", "{model}"],
            models=[ModelOption(id="sonnet", label="Sonnet")],
            default_model="sonnet",
        )

        self.assertEqual(
            _command_with_model(agent, "sonnet"),
            ["claude", "--print", "--model", "sonnet"],
        )

    def test_model_arg_inserts_before_stdin_marker(self):
        agent = AgentConfig(
            id="codex",
            command=["codex", "exec", "-"],
            model_arg=["--model", "{model}"],
            models=[ModelOption(id="gpt-5.4", label="GPT-5.4")],
            default_model="gpt-5.4",
        )

        self.assertEqual(
            _command_with_model(agent, "gpt-5.4"),
            ["codex", "exec", "--model", "gpt-5.4", "-"],
        )

    def test_model_arg_inserts_before_prompt_option(self):
        agent = AgentConfig(
            id="gemini",
            command=["gemini", "--output-format", "text", "--prompt", ""],
            model_arg=["--model", "{model}"],
            models=[
                ModelOption(id="gemini-2.5-pro", label="Gemini 2.5 Pro"),
            ],
            default_model="gemini-2.5-pro",
        )

        self.assertEqual(
            _command_with_model(agent, "gemini-2.5-pro"),
            ["gemini", "--output-format", "text", "--model", "gemini-2.5-pro", "--prompt", ""],
        )

    def test_status_callback_reports_running_and_done(self):
        agent = AgentConfig(
            id="local",
            command=[sys.executable, "-c", "print('ok')"],
            model_arg=[],
            models=[ModelOption(id="test-model", label="Test Model")],
            default_model="test-model",
        )
        events = []

        result = asyncio.run(
            run_agent(
                agent,
                "hello",
                10,
                stage="round1",
                status_callback=lambda *args: events.append(args),
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(events[0][:3], ("round1", "local", "running"))
        self.assertEqual(events[-1][:3], ("round1", "local", "done"))


if __name__ == "__main__":
    unittest.main()
