"""Tests for the read-only CUL firmware update checker."""

import pytest

from custom_components.maxcul.sensor import (
    _current_cul_firmware_version,
    _firmware_update_state,
    _manifest_cul_v3_version,
)


CUL_V3_MANIFEST = {
    "CUL_V3": {
        "version": "1.30",
        "artifacts": [{"file": "CUL_V3.hex", "type": "app"}],
    }
}


def test_manifest_extracts_cul_v3_application_version() -> None:
    assert _manifest_cul_v3_version(CUL_V3_MANIFEST) == "1.30"


@pytest.mark.parametrize(
    ("firmware_response", "expected"),
    [
        ("V 1.29.2 a-culfw Build: 5 (2026-08-01)", "1.29.2"),
        ("V 1.29 CUL868", None),
        (None, None),
    ],
)
def test_current_cul_firmware_version(
    firmware_response: str | None, expected: str | None
) -> None:
    assert _current_cul_firmware_version(firmware_response) == expected


@pytest.mark.parametrize(
    "manifest",
    [
        [],
        {},
        {"CUL_V3": {"version": "latest", "artifacts": []}},
        {
            "CUL_V3": {
                "version": "1.30",
                "artifacts": [{"file": "nanoCUL.hex", "type": "app"}],
            }
        },
    ],
)
def test_manifest_rejects_missing_or_invalid_cul_v3_image(manifest) -> None:
    with pytest.raises(ValueError):
        _manifest_cul_v3_version(manifest)


@pytest.mark.parametrize(
    ("current_version", "release_data", "expected"),
    [
        (
            "V 1.29.2 a-culfw Build: 5 (2026-08-01)",
            {"latest_version": "1.30", "error": None},
            "update_available",
        ),
        (
            "V 1.30.0 a-culfw Build: 6 (2026-09-24)",
            {"latest_version": "1.30", "error": None},
            "up_to_date",
        ),
        (
            "V 1.31 a-culfw Build: 7 (2026-10-01)",
            {"latest_version": "1.30", "error": None},
            "up_to_date",
        ),
        (
            "V 1.29 CUL868",
            {"latest_version": "1.30", "error": None},
            "firmware_unrecognized",
        ),
        (
            "V 1.29.2 a-culfw Build: 5 (2026-08-01)",
            {"latest_version": None, "error": "offline"},
            "check_failed",
        ),
    ],
)
def test_firmware_update_state(
    current_version: str,
    release_data: dict[str, str | None],
    expected: str,
) -> None:
    assert (
        _firmware_update_state("CUL_V3", current_version, release_data)
        == expected
    )


def test_firmware_update_state_waits_for_release_check() -> None:
    assert (
        _firmware_update_state(
            "CUL_V3", "V 1.29 a-culfw Build: 5", None
        )
        == "checking"
    )


def test_firmware_update_state_rejects_unidentified_hardware() -> None:
    assert (
        _firmware_update_state(
            None,
            "V 1.29 a-culfw Build: 5",
            {"latest_version": "1.30", "error": None},
        )
        == "hardware_unidentified"
    )
