"""Verify owner_only, ephemeral, and instance_limit are independently tunable.

These three axes (Pillar 1, ephemeral behavior, Pillar 2) should never
collapse into each other. Any combination should work without side effects.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from helpers import make_interaction as _make_interaction

from cascadeui.state.singleton import get_store
from cascadeui import InstanceLimitError
from cascadeui.views.view import StatefulView


class _RaiseOnLimit:
    async def on_instance_limit(self, error):
        raise error


# // ========================================( Axis Independence )======================================== // #


class TestAxisIndependence:
    """All eight combinations of (owner_only, ephemeral, limited) work."""

    async def test_public_persistent_unlimited(self):
        """owner_only=False, not ephemeral, no limit."""

        class _View(StatefulView):
            owner_only = False

        for i in range(3):
            v = _View(interaction=_make_interaction(user_id=i + 1))
            await v.send()

        assert len(get_store()._active_views) == 3

    async def test_public_persistent_limited(self):
        """owner_only=False, not ephemeral, instance_limit=1."""

        class _View(_RaiseOnLimit, StatefulView):
            owner_only = False
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "reject"

        v1 = _View(interaction=_make_interaction(user_id=1))
        await v1.send()

        v2 = _View(interaction=_make_interaction(user_id=1))
        with pytest.raises(InstanceLimitError):
            await v2.send()

    async def test_owner_only_persistent_unlimited(self):
        """owner_only=True, not ephemeral, no limit."""

        class _View(StatefulView):
            owner_only = True

        v1 = _View(interaction=_make_interaction(user_id=1))
        await v1.send()
        v2 = _View(interaction=_make_interaction(user_id=1))
        await v2.send()

        assert len(get_store()._active_views) == 2

    async def test_owner_only_persistent_limited(self):
        """owner_only=True, not ephemeral, instance_limit=1."""

        class _View(_RaiseOnLimit, StatefulView):
            owner_only = True
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "reject"

        v1 = _View(interaction=_make_interaction(user_id=1))
        await v1.send()

        v2 = _View(interaction=_make_interaction(user_id=1))
        with pytest.raises(InstanceLimitError):
            await v2.send()

    async def test_public_ephemeral_unlimited(self):
        """owner_only=False, ephemeral, no limit."""

        class _View(StatefulView):
            owner_only = False

        for i in range(3):
            v = _View(interaction=_make_interaction(user_id=i + 1))
            await v.send(ephemeral=True)

        assert len(get_store()._active_views) == 3

    async def test_public_ephemeral_limited(self):
        """owner_only=False, ephemeral, instance_limit=1."""

        class _View(_RaiseOnLimit, StatefulView):
            owner_only = False
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "reject"

        v1 = _View(interaction=_make_interaction(user_id=1))
        await v1.send(ephemeral=True)

        v2 = _View(interaction=_make_interaction(user_id=1))
        with pytest.raises(InstanceLimitError):
            await v2.send(ephemeral=True)

    async def test_owner_only_ephemeral_unlimited(self):
        """owner_only=True, ephemeral, no limit."""

        class _View(StatefulView):
            owner_only = True

        v = _View(interaction=_make_interaction(user_id=1))
        await v.send(ephemeral=True)
        assert len(get_store()._active_views) == 1

    async def test_owner_only_ephemeral_limited(self):
        """owner_only=True, ephemeral, instance_limit=1."""

        class _View(_RaiseOnLimit, StatefulView):
            owner_only = True
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "reject"

        v1 = _View(interaction=_make_interaction(user_id=1))
        await v1.send(ephemeral=True)

        v2 = _View(interaction=_make_interaction(user_id=1))
        with pytest.raises(InstanceLimitError):
            await v2.send(ephemeral=True)


# // ========================================( Ephemeral Does Not Affect Limits )======================================== // #


class TestEphemeralDoesNotAffectLimits:
    """Ephemeral is a Discord visibility flag, not an instance constraint.
    Ephemeral and non-ephemeral sends count toward the same limit."""

    async def test_ephemeral_and_regular_share_limit(self):
        """An ephemeral send and a regular send of the same view class
        both count toward instance_limit."""

        class _Mixed(_RaiseOnLimit, StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "reject"

        v1 = _Mixed(interaction=_make_interaction(user_id=1))
        await v1.send()

        v2 = _Mixed(interaction=_make_interaction(user_id=1))
        with pytest.raises(InstanceLimitError):
            await v2.send(ephemeral=True)

    async def test_ephemeral_timeout_respects_user_intent(self):
        """Ephemeral timeout handling (Pillar 3) respects the declared
        timeout and never mugs it upward. Instance limits (Pillar 2) are
        unaffected. With ``auto_refresh_ephemeral`` explicit, the user's
        timeout is preserved exactly -- no clamp down, no clamp up."""

        class _ExplicitOffView(StatefulView):
            timeout = 3600
            instance_limit = 5
            instance_scope = "user_guild"
            auto_refresh_ephemeral = False

        v = _ExplicitOffView(interaction=_make_interaction())
        await v.send(ephemeral=True)
        assert v.timeout == 3600
        assert v.auto_refresh_ephemeral is False

        class _ExplicitOnView(StatefulView):
            timeout = 3600
            instance_limit = 5
            instance_scope = "user_guild"
            auto_refresh_ephemeral = True

        v2 = _ExplicitOnView(interaction=_make_interaction())
        await v2.send(ephemeral=True)
        assert v2.timeout == 3600
        assert v2.auto_refresh_ephemeral is True


# // ========================================( Auto-Refresh Ephemeral Derivation )======================================== // #


class TestAutoRefreshEphemeralDerivation:
    """When ``auto_refresh_ephemeral`` is left at its default (``None``), the
    library derives the flag from ``timeout`` at send() time. ``timeout>900``
    or ``None`` engages the handoff; ``timeout<=900`` declines it."""

    async def test_none_timeout_engages_handoff(self):
        """``timeout=None`` means "never expire"; the handoff is required
        because no timeout exits before the 900s webhook cliff."""

        class _View(StatefulView):
            timeout = None

        v = _View(interaction=_make_interaction())
        await v.send(ephemeral=True)
        assert v.auto_refresh_ephemeral is True

    async def test_long_timeout_engages_handoff(self):
        class _View(StatefulView):
            timeout = 3600

        v = _View(interaction=_make_interaction())
        await v.send(ephemeral=True)
        assert v.timeout == 3600
        assert v.auto_refresh_ephemeral is True

    async def test_short_timeout_declines_handoff(self):
        class _View(StatefulView):
            timeout = 300

        v = _View(interaction=_make_interaction())
        await v.send(ephemeral=True)
        assert v.timeout == 300
        assert v.auto_refresh_ephemeral is False

    async def test_boundary_timeout_declines_handoff(self):
        """timeout==900 sits exactly on the webhook cliff, so the handoff
        is not needed -- the token and the view expire together."""

        class _View(StatefulView):
            timeout = 900

        v = _View(interaction=_make_interaction())
        await v.send(ephemeral=True)
        assert v.auto_refresh_ephemeral is False

    async def test_non_ephemeral_send_does_not_derive(self):
        """Channel sends never touch ``auto_refresh_ephemeral`` because the
        flag only governs the webhook-token handoff."""

        class _View(StatefulView):
            timeout = 3600

        v = _View(interaction=_make_interaction())
        await v.send()
        assert v.auto_refresh_ephemeral is None


# // ========================================( Mixed Attribute Validation )======================================== // #


class TestMixedAttributeValidation:
    """Validation runs per attribute regardless of other attributes set."""

    async def test_invalid_instance_policy_regardless_of_owner_only(self):
        with pytest.raises(ValueError, match="instance_policy"):

            class _Bad(StatefulView):
                owner_only = True
                instance_policy = "invalid"

    async def test_invalid_exit_policy_regardless_of_instance_limit(self):
        with pytest.raises(ValueError, match="exit_policy"):

            class _Bad(StatefulView):
                instance_limit = 5
                exit_policy = "explode"

    async def test_invalid_instance_scope_regardless_of_owner_only(self):
        with pytest.raises(ValueError, match="instance_scope"):

            class _Bad(StatefulView):
                owner_only = False
                instance_scope = "planet"


# // ========================================( Participant Limit vs Instance Limit )======================================== // #


class TestParticipantLimitVsInstanceLimit:
    """participant_limit (per-view capacity) and instance_limit (per-scope
    cardinality) are orthogonal Pillar 2 concerns."""

    async def test_high_participant_limit_does_not_bypass_instance_limit(self):
        """A view with room for 100 participants still respects instance_limit=1."""

        class _BigLobby(_RaiseOnLimit, StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "reject"
            participant_limit = 100

        v1 = _BigLobby(interaction=_make_interaction(user_id=1))
        await v1.send()

        v2 = _BigLobby(interaction=_make_interaction(user_id=1))
        with pytest.raises(InstanceLimitError):
            await v2.send()

    async def test_low_instance_limit_does_not_affect_participant_cap(self):
        """instance_limit=1 does not reduce the participant_limit."""

        class _SmallGame(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            participant_limit = 4

        v = _SmallGame(interaction=_make_interaction(user_id=1, guild_id=100))
        await v.send()

        for uid in [200, 300, 400]:
            assert await v.register_participant(uid) is True

        assert len(v._participants) == 3


# // ========================================( Session Continuity Polarity )======================================== // #


class TestSessionContinuityPolarity:
    """``session_continuity`` splits navigation identity from repeat-open
    continuity. Default False gives each invocation an isolated session
    (UUID-suffixed session_id); True restores class-coalesced sharing."""

    async def test_default_session_id_isolates_repeat_opens(self):
        """Two instances of the same class for the same user get distinct
        session_ids under the default polarity."""

        class _Isolated(StatefulView):
            pass

        v1 = _Isolated(interaction=_make_interaction(user_id=42))
        v2 = _Isolated(interaction=_make_interaction(user_id=42))

        assert v1.session_id != v2.session_id
        shared_prefix = f"{_Isolated._class_session_key()}:user_42:"
        assert v1.session_id.startswith(shared_prefix)
        assert v2.session_id.startswith(shared_prefix)
        # 8 hex characters from uuid4().hex[:8].
        assert len(v1.session_id) == len(shared_prefix) + 8
        assert len(v2.session_id) == len(shared_prefix) + 8

    async def test_opt_in_coalesces_repeat_opens(self):
        """``session_continuity = True`` drops the UUID suffix so repeat
        opens of the same class for the same user share a session_id."""

        class _Continuous(StatefulView):
            session_continuity = True

        v1 = _Continuous(interaction=_make_interaction(user_id=42))
        v2 = _Continuous(interaction=_make_interaction(user_id=42))

        assert v1.session_id == v2.session_id
        assert v1.session_id == f"{_Continuous._class_session_key()}:user_42"

    async def test_explicit_session_id_kwarg_wins(self):
        """Passing ``session_id=`` at construction bypasses both derivation
        paths regardless of ``session_continuity``."""

        class _Isolated(StatefulView):
            pass

        class _Continuous(StatefulView):
            session_continuity = True

        v1 = _Isolated(interaction=_make_interaction(user_id=42), session_id="manual-x")
        v2 = _Continuous(interaction=_make_interaction(user_id=42), session_id="manual-x")

        assert v1.session_id == "manual-x"
        assert v2.session_id == "manual-x"

    async def test_session_continuity_validated_as_bool(self):
        """Non-bool ``session_continuity`` values are rejected at class
        definition time via the ``_BOOL_ATTRS`` validator."""

        with pytest.raises(ValueError, match="session_continuity"):

            class _Bad(StatefulView):
                session_continuity = "yes"

    async def test_push_pop_chain_inherits_session_id(self):
        """Navigation identity is independent of the polarity flip:
        pushed views inherit session_id from the parent, so the chain
        stays on one session even when the root uses per-instance UUIDs."""

        class _Parent(StatefulView):
            pass

        class _Child(StatefulView):
            pass

        parent = _Parent(interaction=_make_interaction(user_id=42))
        child = _Child(
            interaction=_make_interaction(user_id=42),
            session_id=parent.session_id,
        )
        assert child.session_id == parent.session_id
