"""The /admin command owned by the management feature."""

from __future__ import annotations

import shlex

from yukibot.kernel import CommandResult, ControlCommand, ModuleNotFoundError

from .service import ManagementService

ADMIN_HELP = """管理命令:
/admin admin list - 列出当前账号和委派管理员
/admin admin add <user_id> - 添加委派管理员
/admin admin remove <user_id> - 删除委派管理员
/admin module list - 列出可管理模块及其状态
/admin module enable <name> - 启用模块
/admin module disable <name> - 停用模块"""


class ManagementCommands:
    def __init__(self, service: ManagementService) -> None:
        self._service = service

    async def handle(self, command: ControlCommand) -> CommandResult:
        try:
            arguments = shlex.split(command.raw_arguments)
        except ValueError as error:
            return CommandResult(f"Invalid arguments: {error}")
        if not arguments or arguments == ["help"]:
            return CommandResult(ADMIN_HELP)
        try:
            if arguments == ["admin", "list"]:
                owner, admins = await self._service.list_admins()
                lines = [f"owner: {owner}"]
                lines.extend(f"admin: {user_id}" for user_id in admins)
                return CommandResult("\n".join(lines))
            if len(arguments) == 3 and arguments[:2] == ["admin", "add"]:
                user_id = int(arguments[2])
                await self._service.add_admin(command, user_id)
                return CommandResult(f"Administrator {user_id} is enabled.")
            if len(arguments) == 3 and arguments[:2] == ["admin", "remove"]:
                user_id = int(arguments[2])
                await self._service.remove_admin(command, user_id)
                return CommandResult(f"Administrator {user_id} is removed.")
            if arguments == ["module", "list"]:
                modules = await self._service.list_modules()
                lines = [
                    f"{module.name}: enabled={str(module.enabled).lower()}, "
                    f"running={str(module.running).lower()}"
                    for module in modules
                ]
                return CommandResult("\n".join(lines) or "No manageable modules.")
            if len(arguments) == 3 and arguments[:2] == ["module", "enable"]:
                module = await self._service.enable_module(arguments[2])
                return CommandResult(f"Module {module.name} is enabled and running.")
            if len(arguments) == 3 and arguments[:2] == ["module", "disable"]:
                module = await self._service.disable_module(arguments[2])
                return CommandResult(f"Module {module.name} is disabled.")
        except ValueError as error:
            return CommandResult(str(error))
        except PermissionError as error:
            return CommandResult(str(error))
        except ModuleNotFoundError as error:
            return CommandResult(error.args[0])
        return CommandResult(ADMIN_HELP)
