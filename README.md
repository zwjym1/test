# 🖼️ opencode 图片自动识别：给 DeepSeek 装上免费"眼睛"

> 零成本方案：让**不支持图片**的模型（如 DeepSeek V4 Flash Free）也能通过**粘贴截图直接问答**。
> opencode 自动把图片交给免费视觉模型 `opencode/mimo-v2.5-free` 识别 → 转成文字 → 主模型基于文字回答。
> **全程后台自动，无需切换模型、无需 API Key、完全免费。**

## ⚡ 懒人模式：一段提示词自动配置

不想手动装？把 [`一键配置提示词.txt`](./一键配置提示词.txt) 里的整段内容**直接复制粘贴给 opencode**，
它会自动识别你的模型、自动写配置文件、自动提示重启。**复制 → 粘贴 → 回车 → 重启**，四步搞定。

```
你粘贴一张图片
      │
      ▼
opencode 发现"当前模型不支持图片"且消息里有图片
      │
      ▼
自动把图片发给免费视觉模型 opencode/mimo-v2.5-free
      │
      ▼
拿到文字描述后，用文字替换图片
      │
      ▼
主模型（DeepSeek）看到的就是文字，正常回答
```

## ✨ 特性

- **0 成本**：视觉模型用免费 `opencode/mimo-v2.5-free`（models.dev 确认支持 image 输入，无 API Key）
- **全自动**：粘贴 → 自动识别 → 自动回答，不打扰你
- **中文优化**：`analysisPrompt` 已定制为中文逐字转述，截图里的汉字识别更准确
- **跨平台**：Windows / macOS / Linux 通用
- **无侵入**：主模型本身能看图（Claude / GPT 等）时完全不干扰

## 📦 安装（三步）

### 1. 放置配置文件

把 [`opencode-image-proxy.json`](./opencode-image-proxy.json) 放到全局配置目录：

```
Windows:   C:\Users\你的用户名\.config\opencode\
macOS/Linux: ~/.config/opencode/
```

### 2. 注册插件

打开 `~/.config/opencode/opencode.jsonc`（没有则新建）：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["@sami7786/opencode-image-proxy"]
}
```

### 3. 改成你的模型名 + 重启

把配置里的 `imageIncapableModels` 改成**你正在用的模型**（格式 `provider/model`，opencode 里按 `Tab` 切换时看到的就是这个名称）：

```json
{
  "imageIncapableModels": ["opencode/deepseek-v4-flash-free"]
}
```

**完全退出并重启 opencode**（启动时自动从 npm 安装插件）。

## ✅ 测试

重启后随便粘贴一张图片 + 提问。看到下面这种输出就成功了：

```
[User pasted image: xxx.png]
<视觉模型输出的完整文字描述>
<主模型基于文字回答你的问题>
```

## ⚙️ 配置项说明

| 字段 | 说明 |
| ---- | ---- |
| `imageIncapableModels` | 不支持图片、需要走"图片→文字"代理的模型列表 |
| `imageReaderModel` | 负责识别图片的视觉模型（免费推荐 `opencode/mimo-v2.5-free`） |
| `analysisPrompt` | 给视觉模型的指令，本库已优化为中文逐字转述 |

## 🆘 常见问题

- **粘贴时报 "this model does not support image input"** → 插件未生效。检查：是否完全退出重启、`opencode.jsonc` 的 JSON 是否合法、模型名是否与 Tab 显示一致。
- **识别结果不准** → 换付费视觉模型 `opencode/mimo-v2.5`，或微调 `analysisPrompt`。
- **主模型本身能看图（Claude/GPT/Gemini）** → **不要**把它写进 `imageIncapableModels`，让它直接看原图效果更好。
- **花不花钱** → `mimo-v2.5-free` 是免费模型，方案 0 成本。

## 🔍 原理与致谢

- 核心实现来自开源插件 [sami7786/opencode-image-proxy](https://www.npmjs.com/package/@sami7786/opencode-image-proxy)（MIT）
- 本仓库为其提供了**开箱即用的配置模板**和**中文文档**，0 行代码
- 该方案同样适用于 MiniMax、GLM 等一切不支持图片输入的 opencode 模型

本仓库还收录技能包：[抖音实地数据采集（免登录）](./skills/douyin-field-collect/)。

## 📄 License

[MIT](./LICENSE)
