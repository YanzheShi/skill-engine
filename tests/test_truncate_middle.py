"""truncate_middle 保头折中保尾折叠策略 + 各接入点（format_observation / verify）回归。"""

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skill_engine.execution.tool_exec.truncate import truncate_middle  # noqa: E402
from skill_engine.execution.tool_exec.bash_util import format_observation  # noqa: E402
from skill_engine.execution.tool_exec.verify import _run_verification  # noqa: E402


class TestTruncateMiddle:
    def test_under_limit_returns_unchanged(self):
        s = "hello world"
        assert truncate_middle(s, 100) == s

    def test_over_limit_keeps_head_and_tail(self):
        s = "HEAD_START" + "x" * 20000 + "TAIL_END"
        out = truncate_middle(s, 3000)
        assert out.startswith("HEAD_START")
        assert out.endswith("TAIL_END")
        assert "omitted" in out

    def test_marker_states_folding_amount(self):
        s = "a" * 10000
        out = truncate_middle(s, 1000, label="stdout")
        assert "stdout truncated" in out
        # 标注里的总量应等于原始长度
        assert f"of {len(s)}" in out

    def test_result_within_max_chars(self):
        import random
        random.seed(42)
        for _ in range(50):
            n = random.randint(1, 30000)
            s = "".join(random.choice("abc123\n ") for _ in range(n))
            out = truncate_middle(s, 5000)
            assert len(out) <= 5000

    def test_tail_preserves_pytest_summary_style_content(self):
        s = "log line\n" * 900 + "FAILED tests/test_x.py::test_y - AssertionError"
        out = truncate_middle(s, 2000)
        assert "FAILED tests/test_x.py::test_y - AssertionError" in out

    def test_non_string_input_coerced(self):
        out = truncate_middle(12345, 10)
        assert isinstance(out, str)
        assert len(out) <= 10


class TestFormatObservation:
    def _exec_result(self, **kw):
        base = {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}
        base.update(kw)
        return base

    def test_short_output_unchanged(self):
        obs = format_observation("echo hi", self._exec_result(stdout="hi\n"))
        assert "stdout:\nhi" in obs
        assert "truncated" not in obs

    def test_stdout_middle_folded_tail_kept(self):
        stdout = "HEAD_MARK\n" + "filler\n" * 2000 + "TAIL_MARK"
        obs = format_observation("cat big.log", self._exec_result(stdout=stdout))
        assert obs.startswith("exit_code: 0")
        assert "stdout truncated" in obs
        assert "TAIL_MARK" in obs

    def test_stderr_middle_folded_traceback_tail_kept(self):
        stderr = "Traceback (most recent call last):\n" + "  ...\n" * 2000 + "ValueError: boom"
        obs = format_observation("python x.py", self._exec_result(exit_code=1, stderr=stderr))
        assert "stderr truncated" in obs
        assert "ValueError: boom" in obs

    def test_overall_cap_never_silent(self):
        # stdout + stderr 都超长时整体仍 ≤20000 且带标注（不再静默 [:20000]）
        obs = format_observation(
            "big", self._exec_result(stdout="s" * 15000, stderr="e" * 15000)
        )
        assert len(obs) <= 20000
        assert "truncated" in obs

    def test_sandbox_fields_still_present(self):
        obs = format_observation("ls", self._exec_result(stdout="a\n"), sandbox=True)
        assert "sandbox: on" in obs


class TestVerifyFeedback:
    def _executor(self, stdout="", stderr="", exit_code=1):
        class FakeExecutor:
            def run_step(self, cmd, cwd=None, timeout=None):
                return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr,
                        "timed_out": False}

        return FakeExecutor()

    def test_short_feedback_unchanged(self):
        fb = _run_verification(self._executor(stderr="boom"), Path("."), "pytest -x", 60)
        assert "[自动验证失败]" in fb
        assert "truncated" not in fb

    def test_stderr_tail_kept(self):
        stderr = "Traceback:\n" + "  frame\n" * 3000 + "KeyError: 'field'"
        fb = _run_verification(self._executor(stderr=stderr), Path("."), "pytest", 60)
        # 整体上限 4000，内层 8000 标注可能被外层折叠吞掉；
        # 关键验证：输出带折叠标注（非静默硬切）且尾部异常行完整保留
        assert "truncated" in fb
        assert "KeyError: 'field'" in fb
        assert len(fb) <= 4000

    def test_overall_cap_annotated_not_silent(self):
        fb = _run_verification(
            self._executor(stdout="o" * 8000, stderr="e" * 8000),
            Path("."), "pytest", 60,
        )
        assert len(fb) <= 4000
        assert "truncated" in fb

    def test_success_returns_none(self):
        assert _run_verification(self._executor(exit_code=0), Path("."), "pytest", 60) is None
