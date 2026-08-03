#!/usr/bin/env python3
"""Goal-based guided configuration for Orichum projects."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from .configure_state import (
    ROLE_LABELS,
    ROLE_ORDER,
    WORK_TYPES,
    ConfigurationDraft,
    ConfigurationSnapshot,
    PendingAccount,
    load_configuration_snapshot,
    recommended_selections,
    revalidate_draft,
    review_draft,
    selection_for_choice,
    stack_is_live_compatible,
)
from .model_routing import RoutingError
from .orichum_config import ResolvedConfig
from .terminal_ui import BACK, Choice, TerminalUI, WizardIO

SnapshotLoader = Callable[
    [Mapping[str, Path], ResolvedConfig, Path],
    ConfigurationSnapshot,
]


@dataclass(frozen=True)
class ConfigureServices:
    load_snapshot: SnapshotLoader
    refresh_snapshot: SnapshotLoader
    prepare_account: Callable[[str], object]
    apply_draft: Callable[
        [ConfigurationSnapshot, ConfigurationDraft],
        None,
    ]
    reconcile: Callable[[bool], int]
    verify_project: Callable[[Path], None]


def _default_services(
    paths: Mapping[str, Path],
    config: ResolvedConfig,
    io: WizardIO,
) -> ConfigureServices:
    from . import orichum_cli

    def verify(project: Path) -> None:
        refreshed = orichum_cli.load_control_plane(
            orichum_cli.default_config_paths(Path(paths["config"]))
        )
        if not orichum_cli._setup_project_ready(paths, refreshed, project):
            raise RoutingError("configured project is not ready")

    def reconcile(verbose: bool) -> int:
        diagnostics = orichum_cli.SetupDiagnostics.create(
            paths,
            verbose=verbose,
        )
        try:
            return orichum_cli._reconcile_runtime(diagnostics)
        finally:
            diagnostics.close()

    def choose_credential(
        heading: str,
        choices: tuple[tuple[str, str], ...],
        *,
        default: int = 0,
    ) -> str:
        selected = io.choose(
            heading,
            tuple(Choice(label) for label, _value in choices),
            selected=default,
            searchable=len(choices) > 6,
        )
        if selected == BACK:
            raise RoutingError("account authentication was cancelled")
        return choices[selected][1]

    return ConfigureServices(
        load_snapshot=load_configuration_snapshot,
        refresh_snapshot=load_configuration_snapshot,
        prepare_account=lambda provider: orichum_cli._prepare_provider_account(
            paths,
            config,
            provider,
            chooser=choose_credential,
        ),
        apply_draft=lambda snapshot, draft: orichum_cli._apply_configuration_draft(
            paths,
            config,
            snapshot,
            draft,
        ),
        reconcile=reconcile,
        verify_project=verify,
    )


def _show_review(
    io: WizardIO,
    snapshot: ConfigurationSnapshot,
    draft: ConfigurationDraft,
) -> None:
    review = review_draft(snapshot, draft)
    io.section("Project", (("Folder", str(review.project)),))
    io.section("Accounts", review.account_rows)
    io.section("Models", review.model_rows)
    io.show(review.session_notice)


def _show_advanced(io: WizardIO) -> None:
    io.section(
        "Advanced commands",
        (
            ("Accounts", "orichum provider account --help"),
            ("Providers", "orichum provider --help"),
            ("Models", "orichum stack --help"),
            ("Projects", "orichum context --help"),
        ),
    )


def _pick_model(
    io: WizardIO,
    snapshot: ConfigurationSnapshot,
    *,
    selected_model: str | None = None,
):
    selections = tuple(
        selection_for_choice(snapshot, choice) for choice in snapshot.catalog.choices
    )
    if not selections:
        raise RoutingError("no compatible live model is available")
    selected = next(
        (
            index
            for index, selection in enumerate(selections)
            if selection.model == selected_model
        ),
        0,
    )
    choice = io.choose(
        "Choose a model",
        tuple(
            Choice(
                selection.model,
                detail=(
                    f"{selection.provider.title()} · "
                    f"{', '.join(selection.account_names)}"
                ),
                marker="current" if index == selected else "",
            )
            for index, selection in enumerate(selections)
        ),
        selected=selected,
        searchable=len(selections) > 6,
    )
    if choice == BACK:
        return None
    return selections[choice]


def _models_menu(
    io: WizardIO,
    snapshot: ConfigurationSnapshot,
    draft: ConfigurationDraft,
) -> ConfigurationDraft:
    selected = io.choose(
        "Models and agents",
        (
            Choice("Use Orichum's recommendation"),
            Choice("Use one model for everything"),
            Choice("Choose models by work type"),
            Choice("Customize every role"),
            Choice("Back"),
        ),
    )
    if selected in {BACK, 4}:
        return draft
    if selected == 0:
        recommended = recommended_selections(snapshot)
        updated = draft
        for role in ROLE_ORDER:
            updated = updated.with_roles((role,), recommended[role])
        return updated
    if selected == 1:
        selection = _pick_model(
            io,
            snapshot,
            selected_model=draft.role_models["controller"].model,
        )
        if selection is None:
            return draft
        return draft.with_roles(ROLE_ORDER, selection)
    if selected == 2:
        updated = draft
        for label, roles in WORK_TYPES.items():
            selection = _pick_model(
                io,
                snapshot,
                selected_model=updated.role_models[roles[0]].model,
            )
            if selection is None:
                return updated
            updated = updated.with_roles(roles, selection)
            io.show(f"{label}: {selection.model}")
        return updated

    updated = draft
    while True:
        role_index = io.choose(
            "Customize every role",
            tuple(
                Choice(
                    ROLE_LABELS[role],
                    detail=updated.role_models[role].model,
                )
                for role in ROLE_ORDER
            )
            + (Choice("Back"),),
        )
        if role_index in {BACK, len(ROLE_ORDER)}:
            return updated
        role = ROLE_ORDER[role_index]
        selection = _pick_model(
            io,
            snapshot,
            selected_model=updated.role_models[role].model,
        )
        if selection is not None:
            updated = updated.with_roles((role,), selection)


def _accounts_menu(
    io: WizardIO,
    snapshot: ConfigurationSnapshot,
    draft: ConfigurationDraft,
    config: ResolvedConfig,
    services: ConfigureServices,
) -> ConfigurationDraft:
    selected = io.choose(
        "Accounts and providers",
        (
            Choice("Add an account"),
            Choice("Configure a backup account"),
            Choice("Change account preference"),
            Choice("Enable, disable, or remove an account"),
            Choice("Back"),
        ),
    )
    if selected in {BACK, 4}:
        return draft
    if selected in {2, 3}:
        io.show("Use Advanced for low-level account maintenance in this release.")
        return draft
    primary = None
    if selected == 1:
        project_account_ids = {
            account_id
            for assignment in snapshot.assignments.values()
            for account_id in assignment.account_ids
        }
        active = tuple(
            account
            for account in snapshot.accounts
            if account.state == "active" and account.id in project_account_ids
        )
        if not active:
            raise RoutingError("no project-used primary account is available")
        primary_index = io.choose(
            "Choose the primary account",
            tuple(Choice(account.name) for account in active),
            searchable=len(active) > 6,
        )
        if primary_index == BACK:
            return draft
        primary = active[primary_index]
        provider = primary.provider
    else:
        providers = config.documents["providers"].get("providers")
        if not isinstance(providers, Mapping) or not providers:
            raise RoutingError("provider configuration is unavailable")
        names = tuple(sorted(providers))
        provider_index = io.choose(
            "Choose a provider",
            tuple(Choice(name.title()) for name in names),
            searchable=len(names) > 6,
        )
        if provider_index == BACK:
            return draft
        provider = names[provider_index]
    prepared = services.prepare_account(provider)
    io.show("Authentication is saved securely and can be reused if you cancel.")
    name = io.text("Account name", prepared.suggested_name)
    if primary is not None:
        pending = PendingAccount(
            provider=provider,
            credential_ref=prepared.credential_ref,
            name=name,
            pool=primary.pool,
            priority=max(primary.priority - 50, 0),
            intent="backup",
            primary_id=primary.id,
            primary_name=primary.name,
        )
        updated = draft.with_pending_account(pending)
        stack = snapshot.stacks.stacks[snapshot.target.stack_name]
        locked = tuple(
            candidate.id
            for candidates in (stack.controller, *stack.agents.values())
            for candidate in candidates
            if snapshot.bindings.candidate_accounts.get(candidate.id) == primary.id
        )
        if locked:
            policy = io.choose(
                "This project currently locks a model to the primary account.",
                (
                    Choice(f"Allow {primary.name} with {name} [recommended]"),
                    Choice("Keep the account lock; do not enable automatic backup"),
                ),
            )
            if policy == 0:
                updated = updated.with_binding_removals(locked)
        return updated
    availability = io.choose(
        "Where should this account be available?",
        (
            Choice("Current project"),
            Choice("All shared projects"),
            Choice("Advanced placement"),
        ),
    )
    if availability == 2:
        io.show("Use Advanced to place an account in a custom group.")
        return draft
    pool = snapshot.target.pools[0] if availability == 0 else "shared"
    intent_index = io.choose(
        "How should Orichum use this account?",
        (
            Choice("Preferred"),
            Choice("Additional equal-choice account"),
            Choice("Backup"),
        ),
    )
    intent = ("preferred", "additional", "backup")[intent_index]
    provider_accounts = tuple(
        account
        for account in snapshot.accounts
        if account.provider == provider and account.state == "active"
    )
    primary_priority = max(
        (account.priority for account in provider_accounts),
        default=100,
    )
    priority = (
        100
        if intent == "preferred"
        else primary_priority
        if intent == "additional"
        else max(primary_priority - 50, 0)
    )
    return draft.with_pending_account(
        PendingAccount(
            provider=provider,
            credential_ref=prepared.credential_ref,
            name=name,
            pool=pool,
            priority=priority,
            intent=intent,
        )
    )


def _review_and_apply(
    io: WizardIO,
    paths: Mapping[str, Path],
    config: ResolvedConfig,
    snapshot: ConfigurationSnapshot,
    draft: ConfigurationDraft,
    services: ConfigureServices,
    verbose: bool,
) -> tuple[bool, ConfigurationDraft]:
    _show_review(io, snapshot, draft)
    if not draft.changed:
        try:
            services.verify_project(snapshot.target.root)
        except RoutingError:
            action = io.choose(
                "This project needs local runtime repair.",
                (
                    Choice("Reconcile local services"),
                    Choice("Back"),
                ),
            )
            if action in {BACK, 1}:
                return False, draft
            if services.reconcile(verbose) != 0:
                raise RoutingError("runtime reconciliation failed")
            services.verify_project(snapshot.target.root)
            io.show("Orichum configuration is ready for new sessions.")
            return True, draft
        io.show("No changes are pending. This project is ready.")
        return False, draft
    action = io.choose(
        "Review changes",
        (
            Choice("Apply changes"),
            Choice("Go back"),
            Choice("Cancel"),
        ),
    )
    if action == 2:
        return True, draft
    if action in {BACK, 1}:
        return False, draft
    refreshed = services.refresh_snapshot(paths, config, snapshot.target.root)
    drift = revalidate_draft(snapshot, draft, refreshed.catalog)
    if drift.invalid_roles:
        labels = ", ".join(ROLE_LABELS[role] for role in drift.invalid_roles)
        io.show(f"Live availability changed for: {labels}")
        updated = draft
        for role in drift.invalid_roles:
            selection = _pick_model(
                io,
                refreshed,
                selected_model=updated.role_models[role].model,
            )
            if selection is None:
                return False, updated
            updated = updated.with_roles((role,), selection)
        return False, updated
    services.apply_draft(snapshot, draft)
    if services.reconcile(verbose) != 0:
        raise RoutingError("runtime reconciliation failed")
    services.verify_project(snapshot.target.root)
    io.show("Orichum configuration is ready for new sessions.")
    return True, draft


def _project_settings_menu(
    io: WizardIO,
    snapshot: ConfigurationSnapshot,
    draft: ConfigurationDraft,
) -> ConfigurationDraft:
    selected = io.choose(
        "Project settings",
        (
            Choice("Model profile or stack"),
            Choice("Account availability"),
            Choice("GitHub identity"),
            Choice("Jira configuration"),
            Choice("Another configured project"),
            Choice("Back"),
        ),
    )
    if selected in {BACK, 5}:
        return draft
    if selected != 0:
        io.show(
            "Use Advanced for this project setting while guided support "
            "is being completed."
        )
        return draft
    names = tuple(
        name
        for name in sorted(snapshot.stacks.stacks)
        if stack_is_live_compatible(snapshot, name)
    )
    if draft.project.stack_name not in names:
        io.show(
            f"Current stack {draft.project.stack_name} is not available "
            "with this project's live accounts."
        )
    if not names:
        io.show("No usable model profile or stack is available.")
        return draft
    current = (
        names.index(draft.project.stack_name)
        if draft.project.stack_name in names
        else 0
    )
    stack_index = io.choose(
        "Choose a model profile or stack",
        tuple(
            Choice(
                name,
                marker="current" if name == draft.project.stack_name else "",
            )
            for name in names
        ),
        selected=current,
        searchable=len(names) > 6,
    )
    if stack_index == BACK:
        return draft
    return draft.with_project(replace(draft.project, stack_name=names[stack_index]))


def run_configure(
    paths: Mapping[str, Path],
    config: ResolvedConfig,
    project: Path,
    *,
    verbose: bool = False,
    io: WizardIO | None = None,
    services: ConfigureServices | None = None,
) -> int:
    ui = TerminalUI() if io is None else io
    operations = _default_services(paths, config, ui) if services is None else services
    snapshot = operations.load_snapshot(paths, config, Path(project))
    draft = ConfigurationDraft.from_snapshot(snapshot)
    ui.section(
        "Configuring Orichum",
        (("Project", str(snapshot.target.root)),),
    )
    top_level = (
        Choice("Accounts and providers"),
        Choice("Models and agents"),
        Choice("Project settings"),
        Choice("Review and repair"),
        Choice("Advanced"),
        Choice("Back"),
    )
    while True:
        selected = ui.choose(
            "What would you like to configure?",
            top_level,
        )
        if selected in {BACK, 5}:
            return 0
        if selected == 0:
            draft = _accounts_menu(ui, snapshot, draft, config, operations)
        elif selected == 1:
            draft = _models_menu(ui, snapshot, draft)
        elif selected == 3:
            completed, draft = _review_and_apply(
                ui,
                paths,
                config,
                snapshot,
                draft,
                operations,
                verbose,
            )
            if completed:
                return 0
        elif selected == 2:
            draft = _project_settings_menu(ui, snapshot, draft)
        elif selected == 4:
            _show_advanced(ui)
