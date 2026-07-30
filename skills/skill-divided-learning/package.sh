#!/bin/bash
# 打包脚本：将 skill-divided-learning 打包为 zip，方便分发
cd /data/workspace
zip -r skill-divided-learning.zip skill-divided-learning/ -x "*.DS_Store"
echo "✅ 打包完成: /data/workspace/skill-divided-learning.zip"
