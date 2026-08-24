from types import SimpleNamespace

from yuxi.services.chat_service import _extract_total_tokens, _token_usage_delta


def _state(total_tokens: int):
    return SimpleNamespace(
        values={
            "token_usage": {
                "run_model_usage": {
                    "input_tokens": total_tokens - 10,
                    "output_tokens": 10,
                    "total_tokens": total_tokens,
                }
            }
        }
    )


def test_token_usage_delta_bills_only_tokens_after_checkpoint_baseline():
    before_resume = _extract_total_tokens(_state(120))
    after_resume = _extract_total_tokens(_state(175))

    assert _token_usage_delta(after_resume, before_resume) == 55


def test_token_usage_delta_handles_repeated_resume_loops_without_rebilling_parent():
    checkpoints = [100, 140, 165]

    assert _token_usage_delta(checkpoints[1], checkpoints[0]) == 40
    assert _token_usage_delta(checkpoints[2], checkpoints[1]) == 25
    assert sum(
        _token_usage_delta(current, previous) or 0
        for previous, current in zip(checkpoints, checkpoints[1:])
    ) == 65


def test_token_usage_delta_skips_accounting_when_checkpoint_baseline_is_unavailable():
    assert _token_usage_delta(200, None) is None
    assert _token_usage_delta(None, 100) is None
    assert _token_usage_delta(90, 100) == 0
