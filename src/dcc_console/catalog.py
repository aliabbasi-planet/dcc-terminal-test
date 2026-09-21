"""Catalogue of DCC configuration tests.

Each entry maps a `@display_config` bit of the stored procedure to:
  * the target entity and the procedure arguments it drives,
    * the business meaning displayed to the tester,
  * the single column the harness reads before/after to prove the outcome,
  * the cast used when restoring that column during a rollback.

Table, column, key and cast are declared here and never built from user input,
so the generated verification and restore statements cannot be injected into.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VerifiedColumn:
    """The column whose value proves whether a test changed anything."""

    table: str
    column: str
    key: str
    cast: str

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.column}"


@dataclass(frozen=True)
class TestDefinition:
    """One executable configuration test."""

    key: str
    bit: int
    target: str
    summary: str
    business_meaning: str
    mechanism: str
    value_label: str
    value_options: tuple[str, ...] | None
    proc_args: tuple[str, ...]
    verify: VerifiedColumn
    # Fixed procedure constant that identifies this test within its display bit,
    # e.g. the location extra_function name for Bit 1. Declared here (never built
    # from user input) so the generated EXEC cannot be redirected to another node.
    function_name: str | None = None
    # A concise note surfaced in the CAB report when the mapping to the procedure
    # is inferred rather than confirmed by the procedure owner.
    assumption: str | None = None

    @property
    def json_field(self) -> str:
        return {
            "location": "location_no",
            "terminal": "terminal_identifier",
            "instance": "instance_identifier",
        }[self.target]


TEST_CATALOG: dict[str, TestDefinition] = {
    definition.key: definition
    for definition in (
        TestDefinition(
            key="Bit 1 — DCC Xpress CO (location extra_function)",
            bit=1,
            target="location",
            summary=(
                "Adds or removes the `DCCXpressCO` entry inside "
                "`ccc.location.extra_function` for the selected location."
            ),
            business_meaning=(
                "DCC Xpress Cardholder Offer decides whether a merchant location may present "
                "a currency-conversion offer at the point of sale. Enabling it opts the "
                "location into Xpress CO handling; removing it withdraws the location from "
                "the offer flow. Getting this wrong means either cardholders never see a DCC "
                "offer they are entitled to, or a location offers conversion it is not "
                "contracted for."
            ),
            mechanism=(
                "The procedure rewrites the location's `extra_function` XML document, adding "
                "or dropping the node that represents DCCXpressCO."
            ),
            value_label="Action",
            value_options=("Add", "Remove"),
            proc_args=("@location_json", "@extra_function_name", "@add", "@is_simulation"),
            verify=VerifiedColumn("[ccc].[location]", "extra_function", "location_no", "xml"),
            function_name="DCCXpressCO",
        ),
        TestDefinition(
            key="Bit 1 — DCC Xpress CO Delayed Terminal (location extra_function)",
            bit=1,
            target="location",
            summary=(
                "Adds or removes the `DCCXpressCODT` (Delayed Terminal) entry inside "
                "`ccc.location.extra_function` for the selected location."
            ),
            business_meaning=(
                "DCC Xpress CO Delayed Terminal governs the currency-conversion offer for "
                "delayed/deferred terminal capture flows (for example unattended or store-and-"
                "forward terminals). Enabling it opts those terminals at the location into the "
                "offer; removing it withdraws them. An incorrect setting means delayed-capture "
                "terminals either miss an entitled DCC offer or present one they should not."
            ),
            mechanism=(
                "The procedure rewrites the location's `extra_function` XML document, adding "
                "or dropping the node that represents DCCXpressCODT."
            ),
            value_label="Action",
            value_options=("Add", "Remove"),
            proc_args=("@location_json", "@extra_function_name", "@add", "@is_simulation"),
            verify=VerifiedColumn("[ccc].[location]", "extra_function", "location_no", "xml"),
            function_name="DCCXpressCODT",
            assumption=(
                "The Delayed Terminal variant is exercised through the same "
                "`@extra_function_name` parameter as DCCXpressCO with `@add = 1`. The exact "
                "node name `DCCXpressCODT` should be confirmed with the procedure owner before "
                "this row is treated as a fully verified capability."
            ),
        ),
        TestDefinition(
            key="Bit 1 — DCC Xpress CO Fallback (location extra_function)",
            bit=1,
            target="location",
            summary=(
                "Adds or removes the `DCCXpressCOFallback` entry inside "
                "`ccc.location.extra_function` for the selected location."
            ),
            business_meaning=(
                "DCC Xpress CO Fallback controls whether the location may fall back to a "
                "currency-conversion offer when the primary Xpress CO path is unavailable. "
                "Enabling it keeps the offer available on the fallback route; removing it "
                "suppresses conversion when the primary path cannot run. An incorrect setting "
                "changes cardholder experience during degraded/fallback processing."
            ),
            mechanism=(
                "The procedure rewrites the location's `extra_function` XML document, adding "
                "or dropping the node that represents DCCXpressCOFallback."
            ),
            value_label="Action",
            value_options=("Add", "Remove"),
            proc_args=("@location_json", "@extra_function_name", "@add", "@is_simulation"),
            verify=VerifiedColumn("[ccc].[location]", "extra_function", "location_no", "xml"),
            function_name="DCCXpressCOFallback",
            assumption=(
                "The Fallback variant is exercised through the same `@extra_function_name` "
                "parameter as DCCXpressCO with `@add = 1`. The exact node name "
                "`DCCXpressCOFallback` should be confirmed with the procedure owner before "
                "this row is treated as a fully verified capability."
            ),
        ),
        TestDefinition(
            key="Bit 2 — Config Download Version (terminal)",
            bit=2,
            target="terminal",
            summary=(
                "Proposes a new `configdownload_version` for the selected terminal in "
                "`cccintegrang.emv_terminal`."
            ),
            business_meaning=(
                "The config download version selects which configuration payload a terminal "
                "pulls on its next TMS call. The procedure supports two versions: `1` = "
                "Standard (the baseline DCC configuration) and `2` = ECB DCC (the European "
                "Central Bank conversion-rate variant). An incorrect version can push "
                "unsupported settings to a live payment terminal and take it out of service."
            ),
            mechanism=(
                "The procedure resolves the version description (Standard / ECB DCC) to its "
                "numeric code (1 / 2) and updates `configdownload_version` on the terminal row."
            ),
            value_label="Target version description",
            value_options=("Standard", "ECB DCC"),
            proc_args=("@terminal_json", "@ConfigDownloadVersionDesc", "@is_simulation"),
            verify=VerifiedColumn(
                "[cccintegrang].[emv_terminal]",
                "configdownload_version",
                "terminal_identifier",
                "tinyint",
            ),
        ),
        TestDefinition(
            key="Bit 4 — Firmware Package (terminal)",
            bit=4,
            target="terminal",
            summary=(
                "Assigns a firmware package to the selected terminal. Requires "
                "`[cccintegrang].[spManageEMVTerminalFirmwarePackage]` downstream."
            ),
            business_meaning=(
                "The firmware package governs the software image the terminal installs. DCC "
                "prompting, currency tables and receipt rendering all depend on it. Assigning "
                "an inappropriate package can brick a field terminal or break EMV compliance."
            ),
            mechanism=(
                "The procedure delegates to the firmware management procedure, which stages "
                "the named package against the terminal."
            ),
            value_label="Firmware package name",
            value_options=None,
            proc_args=("@terminal_json", "@FirmwarePackageName", "@is_simulation"),
            verify=VerifiedColumn(
                "[cccintegrang].[emv_terminal]",
                "firmware_version",
                "terminal_identifier",
                "varchar(200)",
            ),
        ),
        TestDefinition(
            key="Bit 8 — DCC Handler Flags (instance)",
            bit=8,
            target="instance",
            summary=(
                "Sets a single DCC handler flag inside the instance package configuration "
                "for the selected instance."
            ),
            business_meaning=(
                "Handler flags switch individual DCC behaviours on an integration instance: "
                "`dccEnable` is the master switch, `dccEnableAuth` and `dccEnableCompletion` "
                "control which message types may carry a conversion, and the NFC flags govern "
                "contactless and single-tap journeys. Enabling the wrong flag can offer "
                "conversion on a transaction type the acquirer does not settle."
            ),
            mechanism=(
                "The procedure edits the named boolean inside the instance's `package_config` "
                "XML and writes the document back."
            ),
            value_label="Handler flag",
            value_options=(
                "dccEnable",
                "dccEnableAuth",
                "dccEnableCompletion",
                "dccEnableNfc",
                "dccEnableNfcSingleTap",
                "dccEnableRefund",
                "dccFlagsEnabled",
            ),
            proc_args=(
                "@instance_json",
                "@Extra_Config_Name",
                "@Config_value",
                "@is_simulation",
            ),
            verify=VerifiedColumn(
                "[cccintegrang].[instance]",
                "package_config",
                "instance_identifier",
                "xml",
            ),
            assumption=(
                "`dccEnableRefund` is treated as a boolean handler flag set to 1, consistent "
                "with the other dccEnable* flags. `dccFlagsEnabled` is also exercised as a "
                "boolean (`@Config_value = 1`); if it is in fact an integer bitmask its exact "
                "numeric semantics must be confirmed with the procedure owner before that "
                "value is treated as a fully verified capability."
            ),
        ),
        TestDefinition(
            key="Bit 16 — DCC Receipt Template (instance)",
            bit=16,
            target="instance",
            summary="Sets the DCC printout/receipt template on the selected instance.",
            business_meaning=(
                "The receipt template controls the wording and layout of the DCC disclosure "
                "printed for the cardholder. Card scheme rules require the exchange rate, "
                "margin and the cardholder's right to pay in local currency to be shown. An "
                "incorrect template is a compliance breach, not just a cosmetic defect."
            ),
            mechanism=(
                "The procedure stores the named template against the instance's printout "
                "configuration inside `package_config`."
            ),
            value_label="Template name",
            value_options=None,
            proc_args=("@instance_json", "@printout_type_Template_DCC", "@is_simulation"),
            verify=VerifiedColumn(
                "[cccintegrang].[instance]",
                "package_config",
                "instance_identifier",
                "xml",
            ),
        ),
    )
}

TEST_KEYS: tuple[str, ...] = tuple(TEST_CATALOG)
