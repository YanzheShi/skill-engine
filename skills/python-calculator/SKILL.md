---
name: python-calculator
description: A simple Python calculator skill that evaluates mathematical expressions safely and returns results with step-by-step explanations.
tags: [calculator, math, python, utility]
---

# Python Calculator

A simple and safe Python calculator skill.

## User Request

$ARGUMENTS

## When to Use

Use this skill when the user:
- Asks to calculate a mathematical expression
- Needs to evaluate a formula
- Wants to compute percentages, powers, square roots, etc.
- Asks for help with arithmetic

## How to Execute

1. Parse the user's mathematical request
2. Write a safe Python expression
3. Execute it using the `!` command syntax
4. Return the result with a brief explanation

## Available Math Operations

- Basic: `+`, `-`, `*`, `/`, `//`, `%`, `**`
- Functions: `math.sqrt()`, `math.sin()`, `math.cos()`, `math.pi`, `math.e`
- Import math module: `import math`

## Safety Rules

- Never execute arbitrary Python code beyond math operations
- Use Python's `eval()` only with sanitized input
- Reject any request involving file I/O, network, or system commands

## Examples

User: "what is 2 to the power of 10?"
Output: `2 ** 10 = 1024`

User: "sqrt of 144"
Output: `math.sqrt(144) = 12.0`

User: "30 percent of 250"
Output: `250 * 0.30 = 75.0`
