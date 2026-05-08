#!/usr/bin/env python3
"""A tiny stdio MCP server example.

This is intentionally minimal. Replace it with real MCP servers for SIEM, EDR,
threat intelligence, ticketing, CMDB, etc.
"""

import asyncio

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

server = Server("local-security-kb")


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="lookup_playbook",
            description="Look up a local SOC playbook by keyword.",
            inputSchema={
                "type": "object",
                "properties": {"keyword": {"type": "string"}},
                "required": ["keyword"],
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name != "lookup_playbook":
        raise ValueError(f"Unknown tool: {name}")
    keyword = str(arguments.get("keyword", "")).lower()
    content = "未找到匹配的本地 playbook。"
    if any(term in keyword for term in ["rundll32", "进程", "process"]):
        content = "rundll32 可疑执行排查：检查父进程、命令行、DLL 路径、签名、哈希、网络连接和用户上下文。"
    elif any(term in keyword for term in ["ioc", "hash", "domain", "ip"]):
        content = "IOC 研判：确认来源可信度、命中范围、首次/末次出现时间、关联资产和威胁情报标签。"
    return [TextContent(type="text", text=content)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
