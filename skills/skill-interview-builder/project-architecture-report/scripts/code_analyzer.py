#!/usr/bin/env python
"""
Project Architecture Analyzer
=============================
辅助脚本：提取项目中的模块/函数调用关系和代码依赖层级。

【待补充说明】
当前为脚本框架，需要用户根据目标项目的语言和结构完善以下功能：
1. 遍历项目目录，识别所有源代码文件
2. 解析源代码文件，提取函数/类定义和调用关系
3. 构建模块间的依赖关系图
4. 输出 JSON 格式的分析结果，供主 Skill 使用

使用方法：
    python code_analyzer.py

输出：
    project_structure.json  - 包含模块、函数、依赖关系的结构化数据

注意事项：
    - 运行完成后会自动清理临时文件
    - 支持的语言可扩展（当前预留了 Python 解析示例）
"""

import os
import json
import re
from pathlib import Path
from collections import defaultdict


# ============================================================
# TODO: 根据项目语言调整文件扩展名
# ============================================================
SUPPORTED_EXTENSIONS = {
    '.py': 'python',
    '.js': 'javascript',
    '.ts': 'typescript',
    '.java': 'java',
    '.go': 'go',
    '.rs': 'rust',
    '.c': 'c',
    '.cpp': 'cpp',
    '.cs': 'csharp',
}

# 需要忽略的目录和文件
IGNORE_DIRS = {'.git', '__pycache__', 'node_modules', 'venv', '.venv', 'dist', 'build', '.tox'}
IGNORE_FILES = {'__init__.py'}


def scan_project_files(root_dir: str) -> list:
    """扫描项目目录，返回所有支持的文件路径列表。"""
    # TODO: 完善文件扫描逻辑，根据需要过滤无关目录
    source_files = []
    root = Path(root_dir)
    for file_path in root.rglob('*'):
        if file_path.is_file():
            rel_path = file_path.relative_to(root)
            parts = file_path.parts
            if any(ignore in parts for ignore in IGNORE_DIRS):
                continue
            if file_path.suffix in SUPPORTED_EXTENSIONS:
                source_files.append(str(rel_path))
    return source_files


def parse_python_file(file_path: str) -> dict:
    """解析 Python 文件，提取函数定义、类定义和导入语句。"""
    # TODO: 完善 Python 解析逻辑
    # 可使用 ast 模块进行更精确的解析
    result = {
        'file': file_path,
        'imports': [],
        'functions': [],
        'classes': [],
        'calls': [],
    }
    
    import_pattern = re.compile(r'^(?:from\s+(\S+)\s+)?import\s+(\S+)')
    func_pattern = re.compile(r'^def\s+(\w+)\s*\(')
    class_pattern = re.compile(r'^class\s+(\w+)')
    call_pattern = re.compile(r'(\w+)\(.*?\)')
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        for line in lines:
            line = line.strip()
            
            # 匹配导入语句
            match = import_pattern.match(line)
            if match:
                result['imports'].append(line)
            
            # 匹配函数定义
            match = func_pattern.match(line)
            if match:
                result['functions'].append(match.group(1))
            
            # 匹配类定义
            match = class_pattern.match(line)
            if match:
                result['classes'].append(match.group(1))
            
            # 匹配函数调用（简单模式，需要完善）
            # matches = call_pattern.findall(line)
            # result['calls'].extend(matches)
    
    except Exception as e:
        print(f"Warning: Cannot parse {file_path}: {e}")
    
    return result


def parse_file(file_path: str, language: str) -> dict:
    """根据文件语言调用对应的解析器。"""
    # TODO: 为不同语言添加解析器
    if language == 'python':
        return parse_python_file(file_path)
    else:
        # 对于其他语言，返回基本文件信息
        return {
            'file': file_path,
            'language': language,
            'imports': [],
            'functions': [],
            'classes': [],
            'calls': [],
        }


def build_dependency_graph(parsed_files: list) -> dict:
    """构建模块间的依赖关系图。"""
    # TODO: 完善依赖图构建逻辑
    # 根据每个文件的 import 语句，建立模块间的依赖关系
    graph = {
        'nodes': [],
        'edges': [],
        'layers': {},
    }
    
    # 提取所有模块名
    for pf in parsed_files:
        graph['nodes'].append({
            'id': pf['file'],
            'functions': pf['functions'],
            'classes': pf['classes'],
        })
    
    # 根据 import 关系建立边
    # TODO: 完善依赖分析逻辑
    
    return graph


def main():
    """主函数：扫描项目并生成分析结果。"""
    print("🔍 开始分析项目代码结构...")
    
    # 使用当前工作目录（由 Claude Code 在项目根目录执行）
    root_dir = os.getcwd()
    print(f"📁 项目目录: {root_dir}")
    
    # Step 1: 扫描文件
    source_files = scan_project_files(root_dir)
    print(f"📄 找到 {len(source_files)} 个源代码文件")
    
    # Step 2: 解析文件
    parsed_files = []
    for sf in source_files:
        ext = Path(sf).suffix
        language = SUPPORTED_EXTENSIONS.get(ext, 'unknown')
        result = parse_file(sf, language)
        parsed_files.append(result)
    
    # Step 3: 构建依赖图
    dependency_graph = build_dependency_graph(parsed_files)
    print(f"🔗 构建依赖图: {len(dependency_graph['nodes'])} 个节点")
    
    # Step 4: 输出结果
    output = {
        'project_root': root_dir,
        'total_files': len(source_files),
        'files': [pf['file'] for pf in parsed_files],
        'dependency_graph': dependency_graph,
        'summary': {
            'total_functions': sum(len(pf['functions']) for pf in parsed_files),
            'total_classes': sum(len(pf['classes']) for pf in parsed_files),
            'total_imports': sum(len(pf['imports']) for pf in parsed_files),
        }
    }
    
    output_path = os.path.join(root_dir, 'project_structure.json')
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    
    print(f"✅ 分析完成！结果已保存到: {output_path}")
    print(f"   - 函数数量: {output['summary']['total_functions']}")
    print(f"   - 类数量: {output['summary']['total_classes']}")
    print(f"   - 导入语句: {output['summary']['total_imports']}")


if __name__ == '__main__':
    main()