#!/usr/bin/env python3
"""Generate native shell completion from the Orichum argparse tree."""

from __future__ import annotations

import argparse
import shlex
from typing import Any


class CompletionError(RuntimeError):
    """The public CLI grammar cannot be rendered safely."""


def set_completion(
    action: argparse.Action,
    kind: str,
) -> argparse.Action:
    if not kind or any(character.isspace() for character in kind):
        raise CompletionError("completion kind is invalid")
    setattr(action, "_orichum_completion", kind)
    return action


def _canonical_option(action: argparse.Action) -> str:
    long_options = [
        option for option in action.option_strings if option.startswith("--")
    ]
    if long_options:
        return max(long_options, key=len)
    if action.option_strings:
        return action.option_strings[-1]
    raise CompletionError("optional action has no option strings")


def _choices(action: argparse.Action) -> list[str] | None:
    if action.choices is None:
        return None
    return [str(choice) for choice in action.choices]


def _takes_value(action: argparse.Action) -> bool:
    return not isinstance(
        action,
        (
            argparse._HelpAction,
            argparse._StoreConstAction,
            argparse._VersionAction,
        ),
    )


def _action_spec(action: argparse.Action) -> dict[str, Any]:
    return {
        "choices": _choices(action),
        "completion": getattr(action, "_orichum_completion", None),
        "help": action.help or "",
        "metavar": action.metavar,
        "nargs": action.nargs,
    }


def _parser_spec(parser: argparse.ArgumentParser) -> dict[str, Any]:
    node: dict[str, Any] = {
        "commands": {},
        "description": parser.description or "",
        "options": {},
        "positionals": [],
        "prog": parser.prog,
        "remainder": False,
        "summary": "",
    }
    for action in parser._actions:
        if action.help is argparse.SUPPRESS:
            continue
        if isinstance(action, argparse._SubParsersAction):
            summaries = {
                choice.dest: choice.help or ""
                for choice in action._choices_actions
            }
            for name, child in action.choices.items():
                child_spec = _parser_spec(child)
                child_spec["summary"] = summaries.get(name, "")
                node["commands"][name] = child_spec
            continue
        if action.option_strings:
            option = _canonical_option(action)
            if option in node["options"]:
                raise CompletionError(f"duplicate completion option: {option}")
            spec = _action_spec(action)
            spec["names"] = list(action.option_strings)
            spec["takes_value"] = _takes_value(action)
            node["options"][option] = spec
            continue
        spec = _action_spec(action)
        spec["dest"] = action.dest
        node["positionals"].append(spec)
        if action.nargs is argparse.REMAINDER:
            node["remainder"] = True
    return node


def completion_spec(parser: argparse.ArgumentParser) -> dict[str, Any]:
    return _parser_spec(parser)


def _nodes(
    spec: dict[str, Any],
    path: tuple[str, ...] = (),
) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
    found = [(path, spec)]
    for name, child in spec["commands"].items():
        found.extend(_nodes(child, (*path, name)))
    return found


def _transitions(
    nodes: list[tuple[tuple[str, ...], dict[str, Any]]],
) -> list[tuple[str, str, str]]:
    transitions: list[tuple[str, str, str]] = []
    for path, node in nodes:
        parent = " ".join(path)
        for name in node["commands"]:
            child = " ".join((*path, name))
            transitions.append((parent, name, child))
    return transitions


def _argument_entries(
    nodes: list[tuple[tuple[str, ...], dict[str, Any]]],
) -> list[tuple[str, dict[str, Any]]]:
    entries: list[tuple[str, dict[str, Any]]] = []
    for path, node in nodes:
        path_key = " ".join(path)
        for option, option_spec in node["options"].items():
            entries.append((f"{path_key}:option:{option}", option_spec))
        for index, positional in enumerate(node["positionals"]):
            entries.append(
                (f"{path_key}:positional:{index}", positional)
            )
    return entries


def _remainder_index(node: dict[str, Any]) -> int | None:
    for index, positional in enumerate(node["positionals"]):
        if positional["nargs"] is argparse.REMAINDER:
            return index
    return None


def _bash_case_patterns(values: list[str]) -> str:
    return "|".join(shlex.quote(value) for value in values)


def _bash_add_values(
    lines: list[str],
    key: str,
    argument: dict[str, Any],
) -> None:
    lines.append(f"    {shlex.quote(key)})")
    choices = argument["choices"] or []
    if choices:
        lines.extend(
            [
                "      while IFS= read -r candidate; do",
                '        [[ -n "$candidate" ]] && COMPREPLY+=("$candidate")',
                "      done < <(compgen -W "
                f"{shlex.quote(' '.join(choices))} -- \"$prefix\" || true)",
            ]
        )
    kind = argument["completion"]
    if kind == "file":
        lines.extend(
            [
                "      while IFS= read -r candidate; do",
                '        [[ -n "$candidate" ]] && COMPREPLY+=("$candidate")',
                '      done < <(compgen -f -- "$prefix" || true)',
            ]
        )
    elif kind == "directory":
        lines.extend(
            [
                "      while IFS= read -r candidate; do",
                '        [[ -n "$candidate" ]] && COMPREPLY+=("$candidate")',
                '      done < <(compgen -d -- "$prefix" || true)',
            ]
        )
    elif kind:
        lines.extend(
            [
                "      while IFS=$'\\t' read -r candidate _description; do",
                '        [[ "$candidate" == "$prefix"* ]] && COMPREPLY+=("$candidate")',
                "      done < <(orichum __complete "
                f"{shlex.quote(kind)} \"$prefix\" 2>/dev/null)",
            ]
        )
    lines.append("      ;;")


def _fish_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _render_bash(spec: dict[str, Any]) -> str:
    nodes = _nodes(spec)
    lines = [
        "# Orichum Bash completion. Generated; do not edit.",
        "_orichum_option() {",
        "  option_takes=0",
        '  option_key=""',
        '  case "$1:$2" in',
    ]
    for path, node in nodes:
        path_key = " ".join(path)
        for option, option_spec in node["options"].items():
            patterns = [
                f"{path_key}:{name}" for name in option_spec["names"]
            ]
            lines.append(f"    {_bash_case_patterns(patterns)})")
            lines.append(
                f"      option_takes={int(option_spec['takes_value'])}"
            )
            lines.append(
                f"      option_key={shlex.quote(f'{path_key}:option:{option}')}"
            )
            lines.append("      ;;")
    lines.extend(
        [
            "  esac",
            "}",
            "_orichum_add_values() {",
            '  local key="$1" prefix="$2" candidate _description',
            '  case "$key" in',
        ]
    )
    for key, argument in _argument_entries(nodes):
        _bash_add_values(lines, key, argument)
    lines.extend(
        [
            "  esac",
            "}",
            "_orichum_complete() {",
            "  local current path token option_token option_prefix i pending_key",
            "  local positional_index remainder_index candidate response_index",
            "  COMPREPLY=()",
            '  current="${COMP_WORDS[COMP_CWORD]}"',
            '  path=""',
            "  positional_index=0",
            '  pending_key=""',
            "  for ((i = 1; i < COMP_CWORD; i++)); do",
            '    token="${COMP_WORDS[i]}"',
            '    if [[ -n "$pending_key" ]]; then',
            '      pending_key=""',
            "      continue",
            "    fi",
            '    [[ "$token" == "--" ]] && return 0',
            '    case "$path:$token" in',
        ]
    )
    for parent, token, child in _transitions(nodes):
        lines.append(
            f"      {shlex.quote(parent + ':' + token)}) "
            f"path={shlex.quote(child)}; positional_index=0; continue ;;"
        )
    lines.extend(
        [
            "    esac",
            '    option_token="${token%%=*}"',
            '    _orichum_option "$path" "$option_token"',
            '    if [[ -n "$option_key" ]]; then',
            '      if (( option_takes )) && [[ "$token" != *=* ]]; then',
            '        pending_key="$option_key"',
            "      fi",
            "      continue",
            "    fi",
            '    [[ "$token" == -* ]] && return 0',
            '    case "$path" in',
        ]
    )
    for path, node in nodes:
        remainder = _remainder_index(node)
        if remainder is None:
            continue
        lines.append(
            f"      {shlex.quote(' '.join(path))}) "
            f"remainder_index={remainder}; "
            "(( positional_index >= remainder_index )) && return 0 ;;"
        )
    lines.extend(
        [
            "    esac",
            "    ((positional_index += 1))",
            "  done",
            '  if [[ -n "$pending_key" ]]; then',
            '    _orichum_add_values "$pending_key" "$current"',
            "    return 0",
            "  fi",
            '  if [[ "$current" == -*=* ]]; then',
            '    option_prefix="${current%%=*}="',
            '    option_token="${option_prefix%=}"',
            '    _orichum_option "$path" "$option_token"',
            '    if [[ -n "$option_key" ]] && (( option_takes )); then',
            '      _orichum_add_values "$option_key" "${current#*=}"',
            '      for response_index in "${!COMPREPLY[@]}"; do',
            '        COMPREPLY[response_index]="$option_prefix${COMPREPLY[response_index]}"',
            "      done",
            "      return 0",
            "    fi",
            "  fi",
            '  case "$path" in',
        ]
    )
    for path, node in nodes:
        remainder = _remainder_index(node)
        if remainder is None:
            continue
        lines.append(f"    {shlex.quote(' '.join(path))})")
        lines.append(f"      if (( positional_index >= {remainder} )); then")
        if remainder > 0:
            lines.append("        return 0")
        lines.append(
            '        [[ -n "$current" && "$current" != -* ]] && return 0'
        )
        option_values = [
            name
            for option in node["options"].values()
            for name in option["names"]
        ]
        lines.extend(
            [
                "        while IFS= read -r candidate; do",
                '          [[ -n "$candidate" ]] && COMPREPLY+=("$candidate")',
                "        done < <(compgen -W "
                f"{shlex.quote(' '.join(option_values))} -- \"$current\" || true)",
                "        return 0",
                "      fi",
            ]
        )
        lines.append("      ;;")
    lines.extend(["  esac", '  case "$path" in'])
    for path, node in nodes:
        values = list(node["commands"])
        values.extend(
            name
            for option in node["options"].values()
            for name in option["names"]
        )
        lines.append(f"    {shlex.quote(' '.join(path))})")
        lines.extend(
            [
                "      while IFS= read -r candidate; do",
                '        [[ -n "$candidate" ]] && COMPREPLY+=("$candidate")',
                "      done < <(compgen -W "
                f"{shlex.quote(' '.join(values))} -- \"$current\" || true)",
                "      ;;",
            ]
        )
    lines.extend(
        [
            "  esac",
            '  _orichum_add_values "$path:positional:$positional_index" "$current"',
            "}",
            "complete -F _orichum_complete orichum",
            "",
        ]
    )
    return "\n".join(lines)


def _zsh_entry(value: str, description: str) -> str:
    escaped_value = value.replace("\\", "\\\\").replace(":", "\\:")
    escaped_description = description.replace("\\", "\\\\").replace(
        ":", "\\:"
    )
    return shlex.quote(f"{escaped_value}:{escaped_description}")


def _zsh_add_values(
    lines: list[str],
    key: str,
    argument: dict[str, Any],
) -> None:
    lines.append(f"    {shlex.quote(key)})")
    choices = argument["choices"] or []
    if choices:
        entries = " ".join(
            _zsh_entry(choice, argument["help"]) for choice in choices
        )
        lines.extend(
            [
                f"      candidates=({entries})",
                '      if [[ -n "$value_prefix" ]]; then',
                "        for ((value_index = 1; value_index <= ${#candidates}; value_index++)); do",
                '          candidates[value_index]="$value_prefix${candidates[value_index]}"',
                "        done",
                "      fi",
                "      _describe 'Orichum values' candidates",
            ]
        )
    kind = argument["completion"]
    if kind == "file":
        lines.extend(
            [
                '      if [[ -n "$value_prefix" ]]; then',
                '        _files -P "$value_prefix"',
                "      else",
                "        _files",
                "      fi",
            ]
        )
    elif kind == "directory":
        lines.extend(
            [
                '      if [[ -n "$value_prefix" ]]; then',
                '        _directories -P "$value_prefix"',
                "      else",
                "        _directories",
                "      fi",
            ]
        )
    elif kind:
        lines.extend(
            [
                "      dynamic=()",
                "      while IFS=$'\\t' read -r candidate description; do",
                '        candidate="${candidate//\\/\\\\}"',
                '        candidate="${candidate//:/\\:}"',
                '        description="${description//\\/\\\\}"',
                '        description="${description//:/\\:}"',
                '        dynamic+=("$candidate:$description")',
                "      done < <(orichum __complete "
                f"{shlex.quote(kind)} \"$prefix\" 2>/dev/null)",
                '      if [[ -n "$value_prefix" ]]; then',
                "        for ((value_index = 1; value_index <= ${#dynamic}; value_index++)); do",
                '          dynamic[value_index]="$value_prefix${dynamic[value_index]}"',
                "        done",
                "      fi",
                "      (( ${#dynamic} )) && _describe 'Orichum values' dynamic",
            ]
        )
    lines.append("      ;;")


def _render_zsh(spec: dict[str, Any]) -> str:
    nodes = _nodes(spec)
    lines = [
        "#compdef orichum",
        "# Orichum zsh completion. Generated; do not edit.",
        "_orichum_option() {",
        "  option_takes=0",
        '  option_key=""',
        '  case "$1:$2" in',
    ]
    for path, node in nodes:
        path_key = " ".join(path)
        for option, option_spec in node["options"].items():
            patterns = [
                f"{path_key}:{name}" for name in option_spec["names"]
            ]
            lines.append(f"    {_bash_case_patterns(patterns)})")
            lines.append(
                f"      option_takes={int(option_spec['takes_value'])}"
            )
            lines.append(
                f"      option_key={shlex.quote(f'{path_key}:option:{option}')}"
            )
            lines.append("      ;;")
    lines.extend(
        [
            "  esac",
            "}",
            "_orichum_add_values() {",
            '  local key="$1" prefix="$2" value_prefix="${3:-}" candidate description',
            "  local -a candidates dynamic",
            "  integer value_index",
            '  case "$key" in',
        ]
    )
    for key, argument in _argument_entries(nodes):
        _zsh_add_values(lines, key, argument)
    lines.extend(
        [
            "  esac",
            "}",
            "_orichum() {",
            "  local current command_path token option_token option_prefix pending_key candidate",
            "  local -a candidates dynamic",
            "  integer i positional_index remainder_index",
            '  command_path=""',
            "  positional_index=0",
            '  pending_key=""',
            '  current="$words[CURRENT]"',
            "  for ((i = 2; i < CURRENT; i++)); do",
            '    token="$words[i]"',
            '    if [[ -n "$pending_key" ]]; then',
            '      pending_key=""',
            "      continue",
            "    fi",
            '    [[ "$token" == "--" ]] && return 0',
            '    case "$command_path:$token" in',
        ]
    )
    for parent, token, child in _transitions(nodes):
        lines.append(
            f"      {shlex.quote(parent + ':' + token)}) "
            f"command_path={shlex.quote(child)}; positional_index=0; continue ;;"
        )
    lines.extend(
        [
            "    esac",
            '    option_token="${token%%=*}"',
            '    _orichum_option "$command_path" "$option_token"',
            '    if [[ -n "$option_key" ]]; then',
            '      if (( option_takes )) && [[ "$token" != *=* ]]; then',
            '        pending_key="$option_key"',
            "      fi",
            "      continue",
            "    fi",
            '    [[ "$token" == -* ]] && return 0',
            '    case "$command_path" in',
        ]
    )
    for path, node in nodes:
        remainder = _remainder_index(node)
        if remainder is None:
            continue
        lines.append(
            f"      {shlex.quote(' '.join(path))}) "
            f"remainder_index={remainder}; "
            "(( positional_index >= remainder_index )) && return 0 ;;"
        )
    lines.extend(
        [
            "    esac",
            "    ((positional_index += 1))",
            "  done",
            '  if [[ -n "$pending_key" ]]; then',
            '    _orichum_add_values "$pending_key" "$current"',
            "    return 0",
            "  fi",
            '  if [[ "$current" == -*=* ]]; then',
            '    option_prefix="${current%%=*}="',
            '    option_token="${option_prefix%=}"',
            '    _orichum_option "$command_path" "$option_token"',
            '    if [[ -n "$option_key" ]] && (( option_takes )); then',
            '      _orichum_add_values "$option_key" "${current#*=}" "$option_prefix"',
            "      return 0",
            "    fi",
            "  fi",
            '  case "$command_path" in',
        ]
    )
    for path, node in nodes:
        remainder = _remainder_index(node)
        if remainder is None:
            continue
        lines.append(f"    {shlex.quote(' '.join(path))})")
        lines.append(f"      if (( positional_index >= {remainder} )); then")
        if remainder > 0:
            lines.append("        return 0")
        lines.append(
            '        [[ -n "$current" && "$current" != -* ]] && return 0'
        )
        entries = " ".join(
            _zsh_entry(name, option["help"])
            for option in node["options"].values()
            for name in option["names"]
        )
        lines.extend(
            [
                f"        candidates=({entries})",
                "        _describe 'Orichum options' candidates",
                "        return 0",
                "      fi",
                "      ;;",
            ]
        )
    lines.extend(["  esac", '  case "$command_path" in'])
    for path, node in nodes:
        entries = [
            _zsh_entry(name, child["summary"])
            for name, child in node["commands"].items()
        ]
        entries.extend(
            _zsh_entry(name, option["help"])
            for option in node["options"].values()
            for name in option["names"]
        )
        lines.append(f"    {shlex.quote(' '.join(path))})")
        lines.extend(
            [
                f"      candidates=({' '.join(entries)})",
                "      _describe 'Orichum commands and options' candidates",
                "      ;;",
            ]
        )
    lines.extend(
        [
            "  esac",
            '  _orichum_add_values "$command_path:positional:$positional_index" "$current"',
            "}",
            "_orichum \"$@\"",
            "",
        ]
    )
    return "\n".join(lines)


def _fish_add_values(
    lines: list[str],
    key: str,
    argument: dict[str, Any],
) -> None:
    lines.append(f"        case {_fish_quote(key)}")
    for choice in argument["choices"] or []:
        record = f"{choice}\\t{argument['help']}"
        lines.append(f"            printf '%b\\n' {_fish_quote(record)}")
    kind = argument["completion"]
    if kind == "file":
        lines.append('            __fish_complete_path "$prefix" file')
    elif kind == "directory":
        lines.append(
            '            __fish_complete_directories "$prefix" directory'
        )
    elif kind:
        lines.append(
            f"            orichum __complete {_fish_quote(kind)} "
            '"$prefix" 2>/dev/null'
        )


def _render_fish(spec: dict[str, Any]) -> str:
    nodes = _nodes(spec)
    lines = [
        "# Orichum fish completion. Generated; do not edit.",
        "function __orichum_values",
        "    set -l key $argv[1]",
        "    set -l prefix $argv[2]",
        "    switch $key",
    ]
    for key, argument in _argument_entries(nodes):
        _fish_add_values(lines, key, argument)
    lines.extend(
        [
            "    end",
            "end",
            "function __orichum_complete",
            "    set -l words (commandline -opc)",
            "    set -l current (commandline -ct)",
            "    set -l path ''",
            "    set -l positional_index 0",
            "    set -l pending_key ''",
            "    for token in $words[2..-1]",
            "        if test -n \"$pending_key\"",
            "            set pending_key ''",
            "            continue",
            "        end",
            "        test \"$token\" = '--'; and return 0",
            "        switch \"$path:$token\"",
        ]
    )
    for parent, token, child in _transitions(nodes):
        lines.extend(
            [
                f"            case {_fish_quote(parent + ':' + token)}",
                f"                set path {_fish_quote(child)}",
                "                set positional_index 0",
                "                continue",
            ]
        )
    lines.extend(
        [
            "        end",
            "        set -l option_token (string replace -r '=.*$' '' -- \"$token\")",
            "        set -l option_takes 0",
            "        set -l option_key ''",
            "        switch \"$path:$option_token\"",
        ]
    )
    for path, node in nodes:
        path_key = " ".join(path)
        for option, option_spec in node["options"].items():
            patterns = " ".join(
                _fish_quote(f"{path_key}:{name}")
                for name in option_spec["names"]
            )
            lines.append(f"            case {patterns}")
            lines.append(
                f"                set option_takes {int(option_spec['takes_value'])}"
            )
            lines.append(
                "                set option_key "
                f"{_fish_quote(f'{path_key}:option:{option}')}"
            )
    lines.extend(
        [
            "        end",
            "        if test -n \"$option_key\"",
            "            if test $option_takes -eq 1; and not string match -q -- '*=*' \"$token\"",
            "                set pending_key \"$option_key\"",
            "            end",
            "            continue",
            "        end",
            "        string match -q -- '-*' \"$token\"; and return 0",
            "        switch $path",
        ]
    )
    for path, node in nodes:
        remainder = _remainder_index(node)
        if remainder is None:
            continue
        lines.extend(
            [
                f"            case {_fish_quote(' '.join(path))}",
                f"                test $positional_index -ge {remainder}; and return 0",
            ]
        )
    lines.extend(
        [
            "        end",
            "        set positional_index (math $positional_index + 1)",
            "    end",
            "    if test -n \"$pending_key\"",
            "        __orichum_values \"$pending_key\" \"$current\"",
            "        return 0",
            "    end",
            "    if string match -q -- '-*=*' \"$current\"",
            "        set option_prefix (string replace -r '=.*$' '=' -- \"$current\")",
            "        set option_token (string replace -r '=.*$' '' -- \"$current\")",
            "        set option_takes 0",
            "        set option_key ''",
            "        switch \"$path:$option_token\"",
        ]
    )
    for path, node in nodes:
        path_key = " ".join(path)
        for option, option_spec in node["options"].items():
            patterns = " ".join(
                _fish_quote(f"{path_key}:{name}")
                for name in option_spec["names"]
            )
            lines.append(f"            case {patterns}")
            lines.append(
                f"                set option_takes {int(option_spec['takes_value'])}"
            )
            lines.append(
                "                set option_key "
                f"{_fish_quote(f'{path_key}:option:{option}')}"
            )
    lines.extend(
        [
            "        end",
            "        if test -n \"$option_key\"; and test $option_takes -eq 1",
            "            for candidate in (__orichum_values \"$option_key\" (string replace -r '^[^=]*=' '' -- \"$current\"))",
            "                printf '%s%s\\n' \"$option_prefix\" \"$candidate\"",
            "            end",
            "            return 0",
            "        end",
            "    end",
            "    switch $path",
        ]
    )
    for path, node in nodes:
        remainder = _remainder_index(node)
        if remainder is None:
            continue
        lines.extend(
            [
                f"        case {_fish_quote(' '.join(path))}",
                f"            if test $positional_index -ge {remainder}",
            ]
        )
        if remainder > 0:
            lines.append("                return 0")
        lines.extend(
            [
                "                if test -n \"$current\"; and not string match -q -- '-*' \"$current\"",
                "                    return 0",
                "                end",
            ]
        )
        for option in node["options"].values():
            for name in option["names"]:
                record = f"{name}\\t{option['help']}"
                lines.append(
                    f"                printf '%b\\n' {_fish_quote(record)}"
                )
        lines.extend(["                return 0", "            end"])
    lines.extend(["    end", "    switch $path"])
    for path, node in nodes:
        lines.append(f"        case {_fish_quote(' '.join(path))}")
        for name, child in node["commands"].items():
            record = f"{name}\\t{child['summary']}"
            lines.append(f"            printf '%b\\n' {_fish_quote(record)}")
        for option in node["options"].values():
            for name in option["names"]:
                record = f"{name}\\t{option['help']}"
                lines.append(f"            printf '%b\\n' {_fish_quote(record)}")
    lines.extend(
        [
            "    end",
            "    __orichum_values \"$path:positional:$positional_index\" \"$current\"",
            "end",
            "complete -c orichum -f -a '(__orichum_complete)'",
            "",
        ]
    )
    return "\n".join(lines)


def render_completion(parser: argparse.ArgumentParser, shell: str) -> str:
    spec = completion_spec(parser)
    if shell == "bash":
        return _render_bash(spec)
    if shell == "zsh":
        return _render_zsh(spec)
    if shell == "fish":
        return _render_fish(spec)
    raise CompletionError(f"unsupported completion shell: {shell}")
