"""Tests for form validation system (validators, validate_field, validate_fields)."""

import pytest

from cascadeui.utils import is_emoji
from cascadeui.validation import (
    ValidationResult,
    choices,
    emoji,
    max_length,
    max_value,
    min_length,
    min_value,
    regex,
    validate_field,
    validate_fields,
)


class TestBuiltInValidators:
    """Built-in validator factories (min_length, max_length, regex, etc.) produce correct results."""

    def test_min_length_pass(self):
        v = min_length(3)
        result = v("hello", {}, {})
        assert result.valid

    def test_min_length_fail(self):
        v = min_length(3)
        result = v("ab", {}, {})
        assert not result.valid

    def test_min_length_none(self):
        v = min_length(1)
        result = v(None, {}, {})
        assert not result.valid

    def test_max_length_pass(self):
        v = max_length(5)
        result = v("hi", {}, {})
        assert result.valid

    def test_max_length_fail(self):
        v = max_length(3)
        result = v("toolong", {}, {})
        assert not result.valid

    def test_max_length_none_is_ok(self):
        v = max_length(5)
        result = v(None, {}, {})
        assert result.valid

    def test_regex_pass(self):
        v = regex(r"^[a-z]+$")
        result = v("hello", {}, {})
        assert result.valid

    def test_regex_fail(self):
        v = regex(r"^[a-z]+$", "lowercase only")
        result = v("Hello123", {}, {})
        assert not result.valid
        assert result.message == "lowercase only"

    def test_choices_pass(self):
        v = choices(["red", "green", "blue"])
        result = v("red", {}, {})
        assert result.valid

    def test_choices_fail(self):
        v = choices(["red", "green", "blue"])
        result = v("yellow", {}, {})
        assert not result.valid

    def test_min_value_pass(self):
        v = min_value(10)
        result = v(15, {}, {})
        assert result.valid

    def test_min_value_fail(self):
        v = min_value(10)
        result = v(5, {}, {})
        assert not result.valid

    def test_max_value_pass(self):
        v = max_value(100)
        result = v(50, {}, {})
        assert result.valid

    def test_max_value_fail(self):
        v = max_value(100)
        result = v(200, {}, {})
        assert not result.valid

    def test_min_value_non_numeric(self):
        v = min_value(5)
        result = v("abc", {}, {})
        assert not result.valid

    def test_numeric_bounds_reject_nan(self):
        # NaN compares False to every bound, so a naive ``< n`` check lets it
        # pass. Both bounds must reject it.
        nan = float("nan")
        assert not min_value(0)(nan, {}, {}).valid
        assert not max_value(100)(nan, {}, {}).valid


class TestValidateField:
    """validate_field runs multiple validators against a single field value."""

    async def test_multiple_validators_on_one_field(self):
        field_def = {
            "id": "username",
            "validators": [min_length(3), max_length(10)],
        }
        errors = await validate_field("ab", field_def, {})
        assert len(errors) == 1  # Only min_length fails

    async def test_all_validators_pass(self):
        field_def = {
            "id": "name",
            "validators": [min_length(2), max_length(20)],
        }
        errors = await validate_field("Alice", field_def, {})
        assert len(errors) == 0

    async def test_multiple_failures(self):
        field_def = {
            "id": "code",
            "validators": [min_length(5), regex(r"^[A-Z]+$", "uppercase only")],
        }
        errors = await validate_field("ab", field_def, {})
        assert len(errors) == 2  # Both fail

    async def test_custom_async_validator(self):
        async def always_fail(value, field, all_values):
            return ValidationResult(False, "Custom async failure")

        field_def = {
            "id": "test",
            "validators": [always_fail],
        }
        errors = await validate_field("anything", field_def, {})
        assert len(errors) == 1
        assert errors[0].message == "Custom async failure"

    async def test_empty_optional_value_skips_validators(self):
        """A blank optional field passes without running its validators.

        Most validators reject blank, so running them on an untouched optional
        field made it secretly required. The required-check owns the blank
        case for required fields separately.
        """
        from cascadeui.validation import min_length

        fd = {"id": "email", "required": False, "validators": [min_length(5)]}
        assert await validate_field(None, fd, {}) == []
        assert await validate_field("   ", fd, {}) == []

    async def test_empty_required_value_still_runs_validators(self):
        from cascadeui.validation import min_length

        fd = {"id": "email", "required": True, "validators": [min_length(5)]}
        assert len(await validate_field(None, fd, {})) == 1

    async def test_zero_and_false_are_values_not_blanks(self):
        # 0 and False are legitimate values; the empty-skip must not treat
        # them as blank and pass a bound they actually violate.
        from cascadeui.validation import min_value

        fd = {"id": "n", "required": False, "validators": [min_value(1)]}
        assert len(await validate_field(0, fd, {})) == 1

    async def test_async_callable_object_is_awaited(self):
        """An object with an async ``__call__`` is the documented awaitable
        shape (a DB-backed uniqueness check), but ``iscoroutinefunction`` is
        False for it, so it was called and never awaited."""

        class AsyncUnique:
            async def __call__(self, value, field, all_values):
                return ValidationResult(False, "taken")

        errors = await validate_field("x", {"id": "u", "validators": [AsyncUnique()]}, {})
        assert [e.message for e in errors] == ["taken"]

    async def test_validator_returning_non_result_raises_directed_error(self):
        """A validator that forgets to return a ValidationResult crashed with
        a far-away AttributeError; it now raises naming the validator and field."""

        def forgot_return(value, field, all_values):
            pass

        with pytest.raises(TypeError, match="ValidationResult"):
            await validate_field("x", {"id": "email", "validators": [forgot_return]}, {})


class TestValidateFields:
    """validate_fields runs validators across multiple fields and collects all errors."""

    async def test_mixed_pass_and_fail(self):
        field_defs = [
            {"id": "name", "validators": [min_length(3)]},
            {"id": "age", "validators": [min_value(18)]},
        ]
        values = {"name": "Al", "age": 25}
        errors = await validate_fields(values, field_defs)

        assert "name" in errors
        assert "age" not in errors

    async def test_all_pass(self):
        field_defs = [
            {"id": "x", "validators": [min_length(1)]},
            {"id": "y", "validators": [min_value(0)]},
        ]
        values = {"x": "hello", "y": 10}
        errors = await validate_fields(values, field_defs)
        assert errors == {}

    async def test_no_validators(self):
        field_defs = [{"id": "plain"}]
        values = {"plain": "anything"}
        errors = await validate_fields(values, field_defs)
        assert errors == {}


# // ========================================( Emoji )======================================== // #


class TestIsEmoji:
    """The is_emoji heuristic accepts emoji and custom tokens, rejects text."""

    def test_unicode_emoji(self):
        assert is_emoji("\U0001f3c6") is True  # trophy

    def test_symbol_range_emoji(self):
        assert is_emoji("⭐") is True  # star
        assert is_emoji("⚡") is True  # lightning

    def test_flag_sequence(self):
        assert is_emoji("\U0001f1fa\U0001f1f8") is True  # US flag

    def test_zwj_sequence(self):
        assert is_emoji("\U0001f468‍\U0001f469‍\U0001f467") is True  # family

    def test_custom_token(self):
        assert is_emoji("<:custom:123456789>") is True
        assert is_emoji("<a:spin:987654321>") is True

    def test_shortcode_rejected(self):
        assert is_emoji(":trophy:") is False

    def test_plain_text_rejected(self):
        assert is_emoji("hello") is False
        assert is_emoji("cafe") is False

    def test_non_ascii_text_rejected(self):
        # Non-ASCII but carries ASCII letters -- not an emoji.
        assert is_emoji("café") is False

    def test_empty_and_whitespace_rejected(self):
        assert is_emoji("") is False
        assert is_emoji("   ") is False
        assert is_emoji(None) is False

    def test_digits_rejected(self):
        assert is_emoji("123") is False

    def test_malformed_token_rejected(self):
        assert is_emoji("<:bad>") is False


class TestEmojiValidator:
    """The emoji() validator factory gates a field on is_emoji."""

    def test_emoji_passes(self):
        assert emoji()("\U0001f3c6", {}, {}).valid is True

    def test_custom_token_passes(self):
        assert emoji()("<:custom:123>", {}, {}).valid is True

    def test_non_emoji_fails(self):
        result = emoji()("not an emoji", {}, {})
        assert result.valid is False
        assert result.message

    def test_empty_passes(self):
        # Empty is allowed; required=/min_length forbid blank, not this validator.
        assert emoji()("", {}, {}).valid is True
        assert emoji()(None, {}, {}).valid is True

    def test_custom_message(self):
        result = emoji(msg="Pick an emoji!")("nope", {}, {})
        assert result.message == "Pick an emoji!"
