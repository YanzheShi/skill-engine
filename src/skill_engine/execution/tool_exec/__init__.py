"""tool_exec：tool_dispatch 内层 agent loop 的工具执行子系统。

重构自原 ``tool_dispatch.py`` 的巨方法：
- ``context`` / ``handler`` / ``registry``：ToolHandler 协议 + ToolContext + 注册表
- ``budget`` / ``parse`` / ``bash_util`` / ``edit_patch`` / ``search`` /
  ``read_util`` / ``verify``：按关注点归位的模块级 helper
- ``handlers/``：每工具一个模块的小 handler
"""
