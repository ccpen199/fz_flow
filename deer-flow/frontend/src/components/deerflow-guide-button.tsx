"use client";

import { useState } from "react";
import {
  BookOpenText,
  Brain,
  Code2,
  FileSearch,
  FileUp,
  Globe,
  Hammer,
  Sparkles,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";

const capabilityItems = [
  {
    icon: Globe,
    title: "深度调研",
    description: "网页检索、抓取、总结，适合做项目分析、竞品调研、资料归纳。",
  },
  {
    icon: Sparkles,
    title: "多步任务编排",
    description: "把复杂任务拆成子任务，按步骤推进，不需要你自己拆流程。",
  },
  {
    icon: Hammer,
    title: "代码与文件产出",
    description: "可生成 `md`、`html`、脚本、网页原型、报告等结果文件。",
  },
  {
    icon: Brain,
    title: "上下文记忆",
    description: "同一线程内持续记住前文，适合连续迭代、补充要求和反复修改。",
  },
  {
    icon: FileSearch,
    title: "工具 / 技能调用",
    description: "可调用搜索、抓取、文件读写、bash 等工具完成真实任务。",
  },
];

const caseRoutes = [
  {
    title: "项目调研 / 技术分析",
    icon: Globe,
    goal: "适合陌生仓库、技术预研、竞品分析、老板汇报。",
    steps: [
      "案例：OpenAI Sora Report、Google A2A Protocol Report、What is MCP?、What is LLM?、How to Use Claude for Deep Research?。",
      "可直接这样用：先调研一个 GitHub 仓库 / 协议 / 技术主题，再让它输出结构化中文报告，最后继续追问风险、对比、落地建议。",
      "示例提示词：调研这个项目，输出项目定位、核心能力、技术栈、目录结构、启动方式、适用场景和风险点，并整理成中文 Markdown。",
    ],
  },
  {
    title: "研究内容 → 生成网页 / 页面成品",
    icon: Sparkles,
    goal: "适合 landing page、介绍页、趋势展示页、活动页。",
    steps: [
      "官网公开案例：Forecast 2026 Agent Trends and Opportunities —— 先做 Deep Research，再生成一个网页。",
      "推荐路线：先研究主题，再要求生成 `index.html` / `styles.css` / `README`，最后在同一线程持续改版。",
      "示例提示词：先研究这个主题并整理卖点和页面结构，再基于报告生成一个可演示的单页网站，最后继续优化视觉风格和模块布局。",
    ],
  },
  {
    title: "数据分析 / 文件上传分析",
    icon: FileUp,
    goal: "适合 Excel、CSV、日志、数据导出表、业务明细表。",
    steps: [
      "官网公开案例：An Exploratory Data Analysis of the Titanic Dataset —— 分析数据集并给出可视化与洞察。",
      "推荐路线：上传文件后，先做数据概览，再做异常/趋势判断，最后生成报告或图表建议。",
      "示例提示词：分析这个 CSV 的字段、缺失值、异常值和整体结构，再给出关键发现、可视化建议，并整理成中文报告。",
    ],
  },
  {
    title: "代码 / 脚本 / 文件产出",
    icon: Code2,
    goal: "适合脚本工具、小型页面、自动化代码、文件化交付。",
    steps: [
      "官方定位明确写了 researches, codes, and creates，并且运行时支持文件读写、bash、沙箱执行。",
      "推荐路线：先描述需求目标，再要求按文件输出代码，最后补 README / 启动方式 / 排障说明。",
      "示例提示词：基于这个需求生成完整代码，拆成多个文件输出，并附上运行命令、依赖说明和常见报错处理方式。",
    ],
  },
  {
    title: "长链路研究 / 多模态内容整理",
    icon: Hammer,
    goal: "适合视频、播客、人物资料、跨来源内容整理。",
    steps: [
      "官网公开案例：Watch Y Combinator's Video then Conduct a Deep Research、Collect and Summarize Dr. Fei-Fei Li's Podcasts。",
      "官网公开案例：Generate a Video Based On the Novel 'Pride and Prejudice'、Doraemon Explains the MOE Architecture。",
      "推荐路线：给 DeerFlow 一个来源集合或内容目标，让它先收集，再总结，最后产出报告、网页、视频脚本、漫画说明等结果。",
    ],
  },
  {
    title: "行业 /市场 /趋势研究",
    icon: Brain,
    goal: "适合行业趋势、市场判断、政策影响、技术演进。",
    steps: [
      "README 公开案例：Bitcoin Price Fluctuations、Quantum Computing Impact on Cryptography、AI Adoption in Healthcare: Influencing Factors。",
      "推荐路线：先做行业研究，再继续问影响因素、风险点、机会点，最后收敛成面向决策的摘要。",
      "示例提示词：研究这个行业主题，输出趋势、驱动因素、风险、机会和建议，并给我一版适合汇报的结论摘要。",
    ],
  },
  {
    title: "人物 /内容汇总 / 体育表现分析",
    icon: FileSearch,
    goal: "适合人物资料、公开内容整理、比赛表现总结。",
    steps: [
      "README 公开案例：Cristiano Ronaldo's Performance Highlights。",
      "官网公开案例：Collect and Summarize Dr. Fei-Fei Li's Podcasts。",
      "示例提示词：收集这个人物最近一段时间的公开内容 / 表现 / 观点，做汇总分析，并输出可阅读的中文报告。",
    ],
  },
];

const quickPrompts = [
  "调研这个 GitHub 仓库，输出项目定位、技术栈、目录结构、启动方式、适用场景和风险点，写成中文 Markdown 报告。",
  "先研究这个主题，再生成一个单页网页成品，输出 `index.html`、`styles.css`、`README`，并继续迭代视觉和内容结构。",
  "我上传了一个 CSV，请先做字段与数据质量概览，再输出关键发现、异常点、图表建议，最后整理成分析报告。",
  "收集这个人物 / 主题最近一段时间的公开内容，做归纳总结，对比核心观点，并输出一份中文总结。",
  "围绕这个行业主题做深度研究，输出趋势、驱动因素、风险、机会和行动建议，再整理成适合老板阅读的摘要。",
];

function CopyPromptButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    if (!navigator?.clipboard) return;
    await navigator.clipboard.writeText(text);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };

  return (
    <Button
      variant="outline"
      size="sm"
      onClick={() => void handleCopy()}
      className="border-white/10 bg-white/5 text-zinc-200 hover:bg-white/10"
    >
      {copied ? "已复制" : "复制提示词"}
    </Button>
  );
}

export function DeerFlowGuideButton() {
  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button
          variant="outline"
          className="fixed right-4 bottom-4 z-50 h-11 rounded-full border-white/15 bg-black/70 px-4 text-white shadow-lg backdrop-blur-md hover:bg-black/85 dark:border-white/15 dark:bg-black/70"
        >
          <BookOpenText className="size-4" />
          使用指导
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[85vh] max-w-5xl overflow-y-auto border-white/10 bg-zinc-950 text-zinc-100 sm:rounded-2xl">
        <DialogHeader>
          <DialogTitle className="text-xl">DeerFlow 中文上手指南</DialogTitle>
          <DialogDescription className="text-zinc-400">
            以下案例来自 DeerFlow 官网公开案例和 GitHub README。不要把任务切碎提问，直接给完整目标更有效。
          </DialogDescription>
        </DialogHeader>

        <section className="space-y-3">
          <h3 className="text-base font-semibold text-white">核心能力</h3>
          <div className="grid gap-3 md:grid-cols-2">
            {capabilityItems.map((item) => {
              const Icon = item.icon;
              return (
                <div key={item.title} className="rounded-xl border border-white/10 bg-white/5 p-4">
                  <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-white">
                    <Icon className="size-4 text-amber-300" />
                    {item.title}
                  </div>
                  <p className="text-sm leading-6 text-zinc-300">{item.description}</p>
                </div>
              );
            })}
          </div>
        </section>

        <section className="space-y-4">
          <div>
            <h3 className="text-base font-semibold text-white">官方公开案例集合</h3>
            <p className="mt-1 text-sm leading-6 text-zinc-400">
              这里把官方公开案例按使用方式集中整理到一块。你可以按同样结构在一个线程里连续推进。
            </p>
          </div>
          <div className="space-y-4">
            {caseRoutes.map((route) => {
              const Icon = route.icon;
              return (
                <div key={route.title} className="rounded-2xl border border-white/10 bg-black/30 p-5">
                  <div className="mb-3 flex items-start gap-3">
                    <div className="mt-0.5 rounded-lg border border-amber-300/20 bg-amber-300/10 p-2">
                      <Icon className="size-4 text-amber-300" />
                    </div>
                    <div className="min-w-0 flex-1">
                      <h4 className="text-base font-semibold text-white">{route.title}</h4>
                      <p className="mt-1 text-sm leading-6 text-zinc-300">{route.goal}</p>
                    </div>
                  </div>
                  <div className="space-y-3">
                    {route.steps.map((step) => (
                      <div key={step} className="rounded-xl border border-white/8 bg-white/5 p-3">
                        <p className="text-sm leading-6 whitespace-pre-wrap text-zinc-200">{step}</p>
                      </div>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </section>

        <section className="space-y-3">
          <h3 className="text-base font-semibold text-white">5 个可直接复制的通用提示词</h3>
          <div className="space-y-3">
            {quickPrompts.map((prompt, index) => (
              <div key={prompt} className="rounded-xl border border-white/10 bg-white/5 p-4">
                <div className="mb-3 flex items-center justify-between gap-3">
                  <div className="text-xs font-medium tracking-wide text-amber-300 uppercase">
                    Prompt {index + 1}
                  </div>
                  <CopyPromptButton text={prompt} />
                </div>
                <p className="text-sm leading-6 whitespace-pre-wrap text-zinc-200">{prompt}</p>
              </div>
            ))}
          </div>
        </section>
      </DialogContent>
    </Dialog>
  );
}
