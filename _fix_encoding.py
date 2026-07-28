# 验证恢复质量（无 emoji）
with open("src/pipeline/graph.py", "r", encoding="utf-8") as f:
    texts = f.readlines()

checks = ["架构全景图", "编排引擎", "决策", "指挥中心", "采集", "分析", "撰写", "评分", "完成"]
full = "".join(texts)
for c in checks:
    if c in full:
        print(f"OK: {c}")
    else:
        print(f"MISS: {c}")

# 前10行
for line in texts[:10]:
    print(line.rstrip()[:100])
