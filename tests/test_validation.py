"""Tests for 7.9 — Form Validation System."""

import pytest

from cascadeui.validation import (
    ValidationResult,
    min_length, max_length, regex, choices,
    min_value, max_value,
    validate_field, validate_fields,
)


class TestBuiltInValidators:
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


class TestValidateField:
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


class TestValidateFields:
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
