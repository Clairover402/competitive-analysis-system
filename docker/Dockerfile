# ============================================================
# 竞品分析系统 — PostgreSQL 定制镜像
# pgvector (向量检索) + zhparser (中文全文检索)
# ============================================================
FROM postgres:16-bookworm

# ---- 换国内镜像源（阿里云），加速 apt 下载（ECS 访问境外源极慢） ----
RUN sed -i "s@deb.debian.org@mirrors.aliyun.com@g" /etc/apt/sources.list.d/debian.sources
# ---- 系统依赖：编译工具 + SCWS 分词库运行时 ----
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        postgresql-server-dev-16 \
        wget \
        git \
        bzip2 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ---- 安装 pgvector（向量检索） ----
RUN apt-get update && \
    apt-get install -y --no-install-recommends postgresql-16-pgvector && \
    rm -rf /var/lib/apt/lists/*

# ---- 编译安装 SCWS（中文分词库，zhparser 依赖） ----
WORKDIR /tmp
RUN wget -q http://www.xunsearch.com/scws/down/scws-1.2.3.tar.bz2 && \
    tar -xjf scws-1.2.3.tar.bz2 && \
    cd scws-1.2.3 && \
    ./configure --prefix=/usr/local && \
    make && make install && \
    cd /tmp && rm -rf scws-1.2.3*

# ---- 编译安装 zhparser（PG 中文全文检索扩展） ----
# 用 gitclone.com 镜像拉官方原版（ECS 访问 GitHub 不通，gitee/mirrors 已下线）
RUN git clone --depth 1 https://gitclone.com/github.com/amutu/zhparser.git && \
    cd zhparser && \
    make && make install && \
    cd /tmp && rm -rf zhparser

# ---- 清理编译依赖，保留运行时库 ----
RUN apt-get remove -y build-essential postgresql-server-dev-16 wget git bzip2 && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# ---- 共享库路径写入 ldconfig ----
RUN echo "/usr/local/lib" > /etc/ld.so.conf.d/scws.conf && ldconfig
