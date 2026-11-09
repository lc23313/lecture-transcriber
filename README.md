# Lecture Transcriber

个人讲座录音与中英混合转录工具。网页：https://lc23313.github.io/lecture-transcriber/

## GitHub Pages 的边界

Pages 只运行静态前端，不运行 Python 后端。网页可直接本地录音、离线保存及导出；实时识别、完整稿、词汇和整理需要另行部署 `backend/` 中的 HTTPS 私人后端。

在网页 API 设置中填写后端根地址，再输入私人访问口令。私人口令只保留当前会话，百炼 Key 始终放在后端。本仓库不包含密钥、用户音频、稿件或私人配置，也不将音频存入 GitHub。

## 后端连接

按 backend/compose.yaml 部署，设置自己的 LECTURE_DOMAIN、随机 LT_ACCESS_TOKEN、LT_FRONTEND_ORIGIN=https://lc23313.github.io，并将百炼配置放在 backend/runtime-config/config.json。GitHub Pages URL 不是后端地址。没有服务器时保留本地采集模式。

代码仅供个人使用。临时上传 1GB/48小时，服务商只建议开发测试；iPhone 真机和三小时耐久尚未全部验收。不承诺后台持续录音。
