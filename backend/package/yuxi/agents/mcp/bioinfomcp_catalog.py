"""BioinfoMCP 工具目录（生成文件，勿手改）。

由 scripts/gen_bioinfomcp_tools.py 从 docker/mcp/bioinfomcp-tools/manifest.json
生成：上游 florensiawidjaja/BioinfoMCP 固定提交 7ada791，每工具一个隔离镜像、
一张 MCP 卡片。镜像构建：docker compose --profile bioinfomcp build <slug>-image。
"""

from typing import Any

from yuxi.agents.mcp.registry import SOURCE_TYPE_BUILTIN

BIOINFOMCP_COMMIT = "7ada7918b9e515604d3c0ae264d3a9af10bf6e54"
BIOINFOMCP_RUNTIME_SCHEMA = "2"

BIOINFOMCP_SERVERS: dict[str, dict[str, Any]] = {
    "bioinfomcp-bamcoverage": {
        "name": "BioinfoMCP · bamCoverage",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-bamcoverage"],
        "transport": "stdio",
        "description": "隔离运行 bamCoverage：将 BAM 转换为 deeptools 覆盖度信号文件；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 7200,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_bamCoverage"
        ),
    },
    "bioinfomcp-bcftools": {
        "name": "BioinfoMCP · bcftools",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-bcftools"],
        "transport": "stdio",
        "description": "隔离运行 bcftools：处理 VCF/BCF 变异调用文件；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 7200,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_bcftools"
        ),
    },
    "bioinfomcp-bedtools-coverage": {
        "name": "BioinfoMCP · bedtools_coverage",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-bedtools-coverage"],
        "transport": "stdio",
        "description": "隔离运行 bedtools_coverage：计算 BED 区间的覆盖度统计；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 1800,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_bedtools_coverage"
        ),
    },
    "bioinfomcp-bedtools-intersect": {
        "name": "BioinfoMCP · bedtools_intersect",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-bedtools-intersect"],
        "transport": "stdio",
        "description": "隔离运行 bedtools_intersect：计算基因组区间交集；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 1800,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_bedtools_intersect"
        ),
    },
    "bioinfomcp-bowtie2": {
        "name": "BioinfoMCP · bowtie2",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-bowtie2"],
        "transport": "stdio",
        "description": "隔离运行 bowtie2：将短序列比对到参考基因组；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 7200,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_bowtie2"
        ),
    },
    "bioinfomcp-bwa": {
        "name": "BioinfoMCP · bwa",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-bwa"],
        "transport": "stdio",
        "description": "隔离运行 bwa：将测序读段比对到参考基因组；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 7200,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_bwa"
        ),
    },
    "bioinfomcp-computegcbias": {
        "name": "BioinfoMCP · computeGCBias",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-computegcbias"],
        "transport": "stdio",
        "description": "隔离运行 computeGCBias：计算测序数据的 GC 偏倚；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 7200,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_computeGCBias"
        ),
    },
    "bioinfomcp-correctgcbias": {
        "name": "BioinfoMCP · correctGCBias",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-correctgcbias"],
        "transport": "stdio",
        "description": "隔离运行 correctGCBias：校正测序数据的 GC 偏倚；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 7200,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_correctGCBias"
        ),
    },
    "bioinfomcp-cutadapt": {
        "name": "BioinfoMCP · cutadapt",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-cutadapt"],
        "transport": "stdio",
        "description": "隔离运行 cutadapt：去除测序接头的剪切工具；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 1800,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_cutadapt"
        ),
    },
    "bioinfomcp-fatotwobit": {
        "name": "BioinfoMCP · faToTwoBit",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-fatotwobit"],
        "transport": "stdio",
        "description": "隔离运行 faToTwoBit：将 FASTA 转换为 2bit 压缩格式；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 1800,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_faToTwoBit"
        ),
    },
    "bioinfomcp-fastp": {
        "name": "BioinfoMCP · fastp",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-fastp"],
        "transport": "stdio",
        "description": "隔离运行 fastp：FASTQ 快速质控与过滤；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 1800,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_fastp"
        ),
    },
    "bioinfomcp-flye": {
        "name": "BioinfoMCP · flye",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-flye"],
        "transport": "stdio",
        "description": "隔离运行 flye：长读段基因组从头组装；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 21600,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_flye"
        ),
    },
    "bioinfomcp-freebayes": {
        "name": "BioinfoMCP · freebayes",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-freebayes"],
        "transport": "stdio",
        "description": "隔离运行 freebayes：基于单倍型的遗传变异检测；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 7200,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_freebayes"
        ),
    },
    "bioinfomcp-gatk-applybqsr": {
        "name": "BioinfoMCP · gatk_ApplyBQSR",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-gatk-applybqsr"],
        "transport": "stdio",
        "description": "隔离运行 gatk_ApplyBQSR：GATK 应用碱基质量校正；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 21600,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_gatk_ApplyBQSR"
        ),
    },
    "bioinfomcp-gatk-baserecalibrator": {
        "name": "BioinfoMCP · gatk_BaseRecalibrator",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-gatk-baserecalibrator"],
        "transport": "stdio",
        "description": "隔离运行 gatk_BaseRecalibrator：GATK 碱基质量校正学习；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 21600,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_gatk_BaseRecalibrator"
        ),
    },
    "bioinfomcp-gatk-haplotypecaller": {
        "name": "BioinfoMCP · gatk_HaplotypeCaller",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-gatk-haplotypecaller"],
        "transport": "stdio",
        "description": "隔离运行 gatk_HaplotypeCaller：GATK 单倍型遗传变异检测；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 21600,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_gatk_HaplotypeCaller"
        ),
    },
    "bioinfomcp-gatk-selectvariants": {
        "name": "BioinfoMCP · gatk_SelectVariants",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-gatk-selectvariants"],
        "transport": "stdio",
        "description": "隔离运行 gatk_SelectVariants：GATK 变异位点筛选；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 21600,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_gatk_SelectVariants"
        ),
    },
    "bioinfomcp-gunzip": {
        "name": "BioinfoMCP · gunzip",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-gunzip"],
        "transport": "stdio",
        "description": "隔离运行 gunzip：解压 .gz 压缩文件；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 1800,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_gunzip"
        ),
    },
    "bioinfomcp-hisat2": {
        "name": "BioinfoMCP · hisat2",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-hisat2"],
        "transport": "stdio",
        "description": "隔离运行 hisat2：将 RNA-seq 读段比对到参考基因组；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 7200,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_hisat2"
        ),
    },
    "bioinfomcp-kallisto": {
        "name": "BioinfoMCP · kallisto",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-kallisto"],
        "transport": "stdio",
        "description": "隔离运行 kallisto：RNA-seq 转录本定量；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 7200,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_kallisto"
        ),
    },
    "bioinfomcp-macs3-callpeak": {
        "name": "BioinfoMCP · macs3_callpeak",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-macs3-callpeak"],
        "transport": "stdio",
        "description": "隔离运行 macs3_callpeak：ChIP-seq 峰值调用；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 7200,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_macs3_callpeak"
        ),
    },
    "bioinfomcp-macs3-hmmratac": {
        "name": "BioinfoMCP · macs3_hmmratac",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-macs3-hmmratac"],
        "transport": "stdio",
        "description": "隔离运行 macs3_hmmratac：ATAC-seq 开放染色质区域调用；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 7200,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_macs3_hmmratac"
        ),
    },
    "bioinfomcp-mafft": {
        "name": "BioinfoMCP · mafft",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-mafft"],
        "transport": "stdio",
        "description": "隔离运行 mafft：多序列比对；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 1800,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_mafft"
        ),
    },
    "bioinfomcp-meme": {
        "name": "BioinfoMCP · meme",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-meme"],
        "transport": "stdio",
        "description": "隔离运行 meme：motif 发现与分析；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 21600,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_meme"
        ),
    },
    "bioinfomcp-minimap2": {
        "name": "BioinfoMCP · minimap2",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-minimap2"],
        "transport": "stdio",
        "description": "隔离运行 minimap2：长读段快速比对；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 7200,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_minimap2"
        ),
    },
    "bioinfomcp-multiqc": {
        "name": "BioinfoMCP · multiqc",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-multiqc"],
        "transport": "stdio",
        "description": "隔离运行 multiqc：聚合多个质控工具的汇总报告；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 1800,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_multiqc"
        ),
    },
    "bioinfomcp-plotcorrelation": {
        "name": "BioinfoMCP · plotCorrelation",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-plotcorrelation"],
        "transport": "stdio",
        "description": "隔离运行 plotCorrelation：绘制样本间相关性图；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 7200,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_plotCorrelation"
        ),
    },
    "bioinfomcp-qualimap": {
        "name": "BioinfoMCP · qualimap",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-qualimap"],
        "transport": "stdio",
        "description": "隔离运行 qualimap：比对结果质量评估；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 7200,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_qualimap"
        ),
    },
    "bioinfomcp-quast": {
        "name": "BioinfoMCP · quast",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-quast"],
        "transport": "stdio",
        "description": "隔离运行 quast：基因组组装质量评估；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 21600,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_quast"
        ),
    },
    "bioinfomcp-salmon": {
        "name": "BioinfoMCP · salmon",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-salmon"],
        "transport": "stdio",
        "description": "隔离运行 salmon：转录本定量；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 7200,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_salmon"
        ),
    },
    "bioinfomcp-samtools": {
        "name": "BioinfoMCP · samtools",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-samtools"],
        "transport": "stdio",
        "description": "隔离运行 samtools：处理 SAM/BAM 比对文件；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 1800,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_samtools"
        ),
    },
    "bioinfomcp-seqtk": {
        "name": "BioinfoMCP · seqtk",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-seqtk"],
        "transport": "stdio",
        "description": "隔离运行 seqtk：FASTQ/FASTA 序列工具包；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 1800,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_seqtk"
        ),
    },
    "bioinfomcp-spades": {
        "name": "BioinfoMCP · spades",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-spades"],
        "transport": "stdio",
        "description": "隔离运行 spades：短读段基因组从头组装；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 21600,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_spades"
        ),
    },
    "bioinfomcp-star": {
        "name": "BioinfoMCP · star",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-star"],
        "transport": "stdio",
        "description": "隔离运行 star：RNA-seq 高速比对；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 21600,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_star"
        ),
    },
    "bioinfomcp-stringtie": {
        "name": "BioinfoMCP · stringtie",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-stringtie"],
        "transport": "stdio",
        "description": "隔离运行 stringtie：转录本组装与定量；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 1800,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_stringtie"
        ),
    },
    "bioinfomcp-trim-galore": {
        "name": "BioinfoMCP · trim-galore",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-trim-galore"],
        "transport": "stdio",
        "description": "隔离运行 trim-galore：测序数据质量剪切；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 1800,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_trim-galore"
        ),
    },
    "bioinfomcp-trimmomatic": {
        "name": "BioinfoMCP · trimmomatic",
        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",
        "args": ["bioinfomcp-trimmomatic"],
        "transport": "stdio",
        "description": "隔离运行 trimmomatic： Illumina 读段剪切；"
        "来源 florensiawidjaja/BioinfoMCP 固定提交 7ada791",
        "icon": "🧬",
        "tags": ["内置", "BioinfoMCP"],
        "timeout": 1800,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": (
            "https://github.com/florensiawidjaja/BioinfoMCP@"
            f"7ada7918b9e515604d3c0ae264d3a9af10bf6e54#mcp_trimmomatic"
        ),
    },
}

BIOINFOMCP_SLUGS = frozenset(BIOINFOMCP_SERVERS)
BIOINFOMCP_EXPECTED_TOOLS: dict[str, tuple[str, ...]] = {
    "bioinfomcp-bamcoverage": ('bamCoverage',),
    "bioinfomcp-bcftools": ('bcftools_annotate', 'bcftools_call', 'bcftools_view', 'bcftools_index', 'bcftools_concat', 'bcftools_query', 'bcftools_stats', 'bcftools_sort', 'bcftools_plugin'),
    "bioinfomcp-bedtools-coverage": ('bedtools_coverage',),
    "bioinfomcp-bedtools-intersect": ('bedtools_intersect',),
    "bioinfomcp-bowtie2": ('bowtie2_align', 'bowtie2_build', 'bowtie2_inspect'),
    "bioinfomcp-bwa": ('bwa_index', 'bwa_mem', 'bwa_aln', 'bwa_samse', 'bwa_sampe', 'bwa_bwasw'),
    "bioinfomcp-computegcbias": ('computeGCBias',),
    "bioinfomcp-correctgcbias": ('correctGCBias',),
    "bioinfomcp-cutadapt": ('cutadapt',),
    "bioinfomcp-fatotwobit": ('faToTwoBit',),
    "bioinfomcp-fastp": ('fastp',),
    "bioinfomcp-flye": ('flye',),
    "bioinfomcp-freebayes": ('freebayes',),
    "bioinfomcp-gatk-applybqsr": ('gatk_ApplyBQSR',),
    "bioinfomcp-gatk-baserecalibrator": ('gatk_BaseRecalibrator',),
    "bioinfomcp-gatk-haplotypecaller": ('gatk_HaplotypeCaller',),
    "bioinfomcp-gatk-selectvariants": ('gatk_SelectVariants',),
    "bioinfomcp-gunzip": ('gunzip', 'gzip', 'zcat'),
    "bioinfomcp-hisat2": ('hisat2_align',),
    "bioinfomcp-kallisto": ('index', 'quant', 'quant_tcc', 'bus', 'h5dump', 'inspect', 'version', 'cite'),
    "bioinfomcp-macs3-callpeak": ('macs3_callpeak',),
    "bioinfomcp-macs3-hmmratac": ('macs3_hmmratac',),
    "bioinfomcp-mafft": ('mafft', 'linsi', 'ginsi', 'einsi', 'fftnsi', 'fftns', 'nwnsi', 'nwns', 'mafft_profile'),
    "bioinfomcp-meme": ('meme',),
    "bioinfomcp-minimap2": ('minimap_index', 'minimap_map', 'minimap_version'),
    "bioinfomcp-multiqc": ('multiqc',),
    "bioinfomcp-plotcorrelation": ('plotCorrelation',),
    "bioinfomcp-qualimap": ('bamqc', 'rnaseq', 'multi_bamqc', 'counts', 'clustering', 'comp_counts'),
    "bioinfomcp-quast": ('quast',),
    "bioinfomcp-salmon": ('salmon_index', 'salmon_quant'),
    "bioinfomcp-samtools": ('samtools_view', 'samtools_sort', 'samtools_index', 'samtools_flagstat', 'samtools_merge', 'samtools_faidx', 'samtools_fastq', 'samtools_flag_convert', 'samtools_quickcheck', 'samtools_stats', 'samtools_depth'),
    "bioinfomcp-seqtk": ('seqtk_seq',),
    "bioinfomcp-spades": ('spades',),
    "bioinfomcp-star": ('star_genome_generate', 'star_align_reads'),
    "bioinfomcp-stringtie": ('stringtie_assemble', 'stringtie_merge', 'stringtie_version'),
    "bioinfomcp-trim-galore": ('trim_galore',),
    "bioinfomcp-trimmomatic": ('trimmomatic_se', 'trimmomatic_pe'),
}
