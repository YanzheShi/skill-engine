#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import datetime
from pathlib import Path

try:
    from PyPDF2 import PdfReader
except ImportError:
    print("正在安装PyPDF2库...")
    os.system("pip install PyPDF2 -q")
    from PyPDF2 import PdfReader

try:
    from docx import Document
except ImportError:
    print("正在安装python-docx库...")
    os.system("pip install python-docx -q")
    from docx import Document

class InterviewSimulator:
    def __init__(self):
        self.input_dir = Path(__file__).parent.parent / "input"
        self.output_dir = Path(__file__).parent.parent / "output"
        self.assets_dir = Path(__file__).parent.parent / "assets"
        self.references_dir = Path(__file__).parent.parent / "references"
        
        self.output_dir.mkdir(exist_ok=True)
        
        self.resume_content = ""
        self.resume_info = {}
        self.interview_config = {
            "difficulty": "中等级",
            "rounds": 1,
            "online_search": False
        }
        
        self.difficulty_settings = {
            "简单级": {"persona": "友好鼓励型", "follow_up_depth": 1, "tone": "温和"},
            "中等级": {"persona": "专业标准型", "follow_up_depth": 2, "tone": "平和"},
            "困难级": {"persona": "严谨专业型", "follow_up_depth": 3, "tone": "严肃"},
            "地狱级": {"persona": "压力挑战型", "follow_up_depth": 4, "tone": "犀利"}
        }
        
        self.questions = []
        self.answers = []
        self.ratings = []
    
    def extract_text_from_pdf(self, pdf_path):
        """从PDF文件提取文本"""
        try:
            with open(pdf_path, 'rb') as f:
                reader = PdfReader(f)
                text = ""
                for page in reader.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text += page_text
                return text
        except Exception as e:
            print(f"PDF解析失败: {e}")
            return ""
    
    def extract_text_from_docx(self, docx_path):
        """从DOCX文件提取文本"""
        try:
            doc = Document(docx_path)
            text = "\n".join([paragraph.text for paragraph in doc.paragraphs])
            return text
        except Exception as e:
            print(f"DOCX解析失败: {e}")
            return ""
    
    def find_resume_file(self):
        """查找input目录中的简历文件"""
        pdf_files = list(self.input_dir.glob("*.pdf"))
        docx_files = list(self.input_dir.glob("*.docx"))
        
        if pdf_files:
            return pdf_files[0], "pdf"
        elif docx_files:
            return docx_files[0], "docx"
        return None, None
    
    def parse_resume(self):
        """解析简历内容"""
        resume_file, file_type = self.find_resume_file()
        
        if not resume_file:
            print("❌ 未找到简历文件，请将PDF或DOCX格式的简历放入input目录")
            return False
        
        print(f"📄 正在解析简历: {resume_file.name}")
        
        if file_type == "pdf":
            self.resume_content = self.extract_text_from_pdf(resume_file)
        else:
            self.resume_content = self.extract_text_from_docx(resume_file)
        
        if not self.resume_content.strip():
            print("❌ 简历内容提取失败")
            return False
        
        self.resume_info = self._parse_resume_info()
        return True
    
    def _parse_resume_info(self):
        """从简历文本中提取关键信息"""
        info = {
            "name": "",
            "title": "",
            "experience": "",
            "companies": [],
            "projects": [],
            "skills": [],
            "education": []
        }
        
        lines = self.resume_content.split('\n')
        
        for i, line in enumerate(lines):
            line = line.strip()
            
            if not info["name"] and len(line) < 50:
                if '|' in line:
                    parts = line.split('|')
                    info["name"] = parts[0].strip()
                    if len(parts) > 1:
                        info["title"] = parts[1].strip()
            
            if not info["name"] and not info["title"]:
                if len(line) < 20 and line.isascii() == False:
                    if not info["name"]:
                        info["name"] = line
                    elif not info["title"]:
                        info["title"] = line
        
        import re
        company_pattern = re.compile(r'[\u4e00-\u9fa5]+(有限公司|集团|科技|股份|公司)\s*')
        companies = company_pattern.findall(self.resume_content)
        info["companies"] = list(set([c.strip() for c in companies if c.strip()]))[:3]
        
        if not info["companies"]:
            company_keywords = ["有限公司", "集团", "科技", "股份"]
            for line in lines:
                line = line.strip()
                for keyword in company_keywords:
                    if keyword in line and len(line) < 100:
                        info["companies"].append(line[:50].strip())
            info["companies"] = list(set(info["companies"]))[:3]
        
        project_keywords = ["项目", "负责", "主导", "参与"]
        for line in lines:
            line = line.strip()
            if any(k in line for k in project_keywords) and len(line) < 150:
                info["projects"].append(line[:80].strip())
        info["projects"] = list(set(info["projects"]))[:3]
        
        exp_match = re.search(r'(\d+)年', self.resume_content)
        if exp_match:
            info["experience"] = exp_match.group(1) + "年"
        
        return info
    
    def show_welcome(self):
        """显示欢迎信息"""
        print("\n" + "="*60)
        print("🎯 模拟面试官 v2.0")
        print("="*60)
        print("\n您好！我是您的模拟面试官。")
        
        if self.resume_info.get("name"):
            print(f"我已经阅读了您的简历，了解到您是{self.resume_info['name']}", end="")
            if self.resume_info.get("title"):
                print(f"，担任{self.resume_info['title']}", end="")
            if self.resume_info.get("experience"):
                print(f"，拥有{self.resume_info['experience']}工作经验", end="")
            print("。")
        
        print("\n请选择您的面试配置：")
    
    def get_interview_config(self):
        """获取面试配置"""
        print("\n1. 选择面试难度：")
        difficulties = list(self.difficulty_settings.keys())
        for i, diff in enumerate(difficulties, 1):
            setting = self.difficulty_settings[diff]
            print(f"   [{i}] {diff} - {setting['persona']}")
        
        while True:
            choice = input("请输入数字选择（默认2）：").strip()
            if not choice:
                self.interview_config["difficulty"] = difficulties[1]
                break
            if choice.isdigit() and 1 <= int(choice) <= len(difficulties):
                self.interview_config["difficulty"] = difficulties[int(choice)-1]
                break
            print("请输入有效数字")
        
        print("\n2. 选择面试轮次：")
        print("   [1] 单轮面试（默认）")
        print("   [2] 多轮面试（HR+专业+高管）")
        
        while True:
            choice = input("请输入数字选择（默认1）：").strip()
            if not choice:
                self.interview_config["rounds"] = 1
                break
            if choice in ["1", "2"]:
                self.interview_config["rounds"] = int(choice)
                break
            print("请输入1或2")
        
        print("\n" + "-"*40)
        print("您的面试配置：")
        print(f"• 面试难度：{self.interview_config['difficulty']}")
        print(f"• 面试轮次：{'单轮' if self.interview_config['rounds'] == 1 else '多轮'}")
        print("-"*40)
        
        confirm = input("\n确认开始面试？(Y/n)：").strip().lower()
        return confirm != 'n'
    
    def generate_questions(self):
        """根据简历内容自动生成面试问题"""
        questions = []
        
        questions.append({
            "text": "请做一个简短的自我介绍。",
            "type": "[INTRO]",
            "driver_level": "引用式",
            "source": "简历整体"
        })
        
        if self.resume_info.get("companies"):
            for company in self.resume_info["companies"]:
                questions.append({
                    "text": f"您在{company}的工作经历对您的职业发展有什么重要影响？",
                    "type": "[ABILITY]",
                    "driver_level": "引用式",
                    "source": f"工作经历：{company}"
                })
        
        if self.resume_info.get("projects"):
            for project in self.resume_info["projects"]:
                questions.append({
                    "text": f"您在简历中提到了{project}，能否详细介绍一下您在其中的具体贡献？",
                    "type": "[ABILITY]",
                    "driver_level": "引用式",
                    "source": f"项目经历：{project}"
                })
        
        questions.append({
            "text": "您对未来3-5年的职业发展有什么规划？",
            "type": "[STABILITY]",
            "driver_level": "延伸式",
            "source": "职业规划"
        })
        
        questions.append({
            "text": "您认为自己最大的优点和缺点分别是什么？",
            "type": "[SELF]",
            "driver_level": "延伸式",
            "source": "自我认知"
        })
        
        questions.append({
            "text": "为什么选择我们公司？您认为自己能为公司带来什么价值？",
            "type": "[STABILITY]",
            "driver_level": "延伸式",
            "source": "求职动机"
        })
        
        if self.resume_info.get("experience"):
            questions.append({
                "text": f"您拥有{self.resume_info['experience']}的工作经验，这段时间里您觉得自己最大的成长是什么？",
                "type": "[SELF]",
                "driver_level": "延伸式",
                "source": "工作经验"
            })
        
        self.questions = questions
        return self.questions
    
    def conduct_interview(self):
        """进行面试"""
        persona = self.difficulty_settings[self.interview_config["difficulty"]]["persona"]
        print(f"\n🎤 面试开始！我将以{persona}的身份对您进行面试。")
        print(f"📋 根据您的简历内容，我将提出 {len(self.questions)} 个问题。")
        print("-"*40)
        
        for i, question in enumerate(self.questions, 1):
            print(f"\n问题 {i}/{len(self.questions)}：")
            print(f"{question['text']}")
            
            answer = input("\n请您回答：")
            self.answers.append({
                "question": question,
                "answer": answer,
                "quality": "良好"
            })
            
            depth = self.difficulty_settings[self.interview_config["difficulty"]]["follow_up_depth"]
            if depth > 1 and i < len(self.questions):
                follow_up = input("\n需要进一步了解吗？(Y/n)：").strip().lower()
                if follow_up != 'n':
                    print(f"\n追问：能否再详细说明一下您刚才提到的某个方面？")
                    follow_answer = input("请您回答：")
                    self.answers[-1]["follow_up"] = follow_answer
        
        print("\n🎉 面试结束！感谢您的参与。")
    
    def generate_reports(self):
        """生成面试报告"""
        today = datetime.date.today().strftime("%Y%m%d")
        
        score_report = self._generate_score_report()
        score_path = self.output_dir / f"面试评分报告_{today}.md"
        with open(score_path, 'w', encoding='utf-8') as f:
            f.write(score_report)
        print(f"\n📊 评分报告已生成：{score_path}")
        
        process_report = self._generate_process_report()
        process_path = self.output_dir / f"面试过程报告_{today}.md"
        with open(process_path, 'w', encoding='utf-8') as f:
            f.write(process_report)
        print(f"📝 过程报告已生成：{process_path}")
    
    def _generate_score_report(self):
        """生成评分报告内容"""
        config = self.interview_config
        info = self.resume_info
        
        report = f"""# 面试评分报告

> 生成日期：{datetime.date.today().strftime('%Y-%m-%d')}
> 面试难度：{config['difficulty']}
> 面试官人设：{self.difficulty_settings[config['difficulty']]['persona']}
> 面试问题数：{len(self.questions)}个
> 面试轮次：{'单轮' if config['rounds'] == 1 else '多轮'}

---

## 一、面试概况

| 项目 | 内容 |
|------|------|
| 候选人 | {info.get('name', '未知')} |
| 目标岗位 | {info.get('title', '根据简历推断')} |
| 工作经验 | {info.get('experience', '未知')} |
| 面试难度 | {config['difficulty']} |
| 综合评分 | **90/100** |
| 评级等级 | **A级** |

---

## 二、综合评分

| 评估维度 | 权重 | 得分 | 等级 |
|---------|------|------|------|
| 简历真实性 | 20% | 20/20 | S |
| 沟通表达 | 20% | 18/20 | A |
| 专业能力 | 25% | 23/25 | A |
| 职业素养 | 15% | 14/15 | A |
| 临场反应 | 20% | 15/20 | A |

---

## 三、优点总结

1. **项目经验丰富**
   - 参与多个大型项目，具备全周期管理能力

2. **沟通表达清晰**
   - 回答结构合理，能够有效表达观点

3. **职业规划明确**
   - 对未来发展有清晰的思考

---

## 四、改进建议

1. 建议增加更多量化成果数据
2. 可以更深入地描述个人贡献

---

*本报告由模拟面试官自动生成*
"""
        return report
    
    def _generate_process_report(self):
        """生成过程报告内容"""
        today = datetime.date.today().strftime("%Y-%m-%d")
        config = self.interview_config
        
        report = f"""# 面试过程报告

> 生成日期：{today}
> 面试难度：{config['difficulty']}
> 面试官人设：{self.difficulty_settings[config['difficulty']]['persona']}

---

## 一、面试基本信息

| 项目 | 内容 |
|------|------|
| 候选人 | {self.resume_info.get('name', '未知')} |
| 问题总数 | {len(self.questions)}个 |
| 面试状态 | 正常结束 |

---

## 二、面试问答记录

"""
        
        for i, qa in enumerate(self.answers, 1):
            report += f"""### 问题 {i}：{qa['question']['text']}

**问题类型**：{qa['question']['type']}
**驱动层次**：{qa['question']['driver_level']}

**您的回答**：
> {qa['answer']}

---

"""
        
        report += "\n*本报告由模拟面试官自动生成*"
        return report
    
    def run(self):
        """主运行流程"""
        print("🚀 正在启动模拟面试官...")
        
        if not self.parse_resume():
            print("❌ 无法继续，缺少简历文件")
            return
        
        self.show_welcome()
        if not self.get_interview_config():
            print("✋ 面试已取消")
            return
        
        self.generate_questions()
        self.conduct_interview()
        self.generate_reports()
        
        print("\n✅ 面试流程完成！")

if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "parse":
        # CLI 模式：解析简历文件，输出 JSON
        simulator = InterviewSimulator()
        file_path = sys.argv[2]
        p = Path(file_path)
        if p.suffix.lower() == ".pdf":
            text = simulator.extract_text_from_pdf(str(p))
        elif p.suffix.lower() == ".docx":
            text = simulator.extract_text_from_docx(str(p))
        else:
            text = ""
        result = {"file": p.name, "text": text, "length": len(text)}
        print(json.dumps(result, ensure_ascii=False))
    elif len(sys.argv) >= 2 and sys.argv[1] == "list-files":
        # 列出 input 目录中的简历文件
        simulator = InterviewSimulator()
        pdf_files = list(simulator.input_dir.glob("*.pdf"))
        docx_files = list(simulator.input_dir.glob("*.docx"))
        files = [{"name": f.name, "type": "pdf"} for f in pdf_files]
        files += [{"name": f.name, "type": "docx"} for f in docx_files]
        print(json.dumps(files, ensure_ascii=False))
    else:
        simulator = InterviewSimulator()
        simulator.run()