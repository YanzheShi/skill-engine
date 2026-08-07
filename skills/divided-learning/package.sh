#!/bin/bash
# 打包脚本：将 divided-learning 打包为 zip，方便分发
cd /data/workspace
zip -r divided-learning.zip divided-learning/ -x "*.DS_Store"
echo "✅ 打包完成: /data/workspace/divided-learning.zip"
