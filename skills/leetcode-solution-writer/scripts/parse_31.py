import json, subprocess, sys, re, html

result = subprocess.run([r'D:\Dev\Python312\python.exe', 'scripts/fetch_problem.py', '31'],
                       capture_output=True, text=True,
                       cwd=r'D:\Code\PycharmProjects\skill-engine\skills\leetcode-solution-writer')
data = json.loads(result.stdout)

title = data['title']
slug = data['slug']
difficulty = data['difficulty']
tags = data['tags']
link = data['link']

content = data['content']
text = re.sub(r'<[^>]+>', ' ', content)
text = html.unescape(text)
text = re.sub(r'\s+', ' ', text).strip()

for snippet in data['codeSnippets']:
    if snippet['langSlug'] == 'cpp':
        cpp_code = snippet['code']
        break

print(f'TITLE: {title}')
print(f'SLUG: {slug}')
print(f'DIFFICULTY: {difficulty}')
print(f'TAGS: {",".join(tags)}')
print(f'LINK: {link}')
print(f'CPP_CODE: {cpp_code}')
print(f'CONTENT: {text}')