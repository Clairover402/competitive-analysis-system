# ============================================================
# 竞品分析系统 — PostgreSQL 定制镜像
# pgvector (向量检索) + zhparser (中文全文检索) + pg_bigm (中文模糊匹配)
# ============================================================
FROM postgres:16-bookworm

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

# ---- 安装 pg_bigm（中文 bigram 模糊匹配） ----
RUN apt-get update && \
    apt-get install -y --no-install-recommends postgresql-16-pg-bigm && \
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
RUN git clone --depth 1 https://github.com/amutu/zhparser.git && \
    cd zhparser && \
    make && make install && \
    cd /tmp && rm -rf zhparser

# ---- 清理编译依赖，保留运行时库 ----
RUN apt-get remove -y build-essential postgresql-server-dev-16 wget git bzip2 && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# ---- 共享库路径写入 ldconfig ----
RUN echo "/usr/local/lib" > /etc/ld.so.conf.d/scws.conf && ldconfig
