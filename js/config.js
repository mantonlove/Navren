const API_CONFIG = {
  endpoint: '/api/chat',
  token: '',
  timeout: 30000,
  useFallback: false,
  rateLimitDelay: 13000,
  models: {
    chat: 'deepseek-v3_2',        // 主力对话（DeepSeek-V3.2）
    multimodal: 'glm-5',           // 图片理解（GLM-5）
    ocr: 'glm-ocr',               // 文档OCR（GLM-OCR）
    reranker: 'qwen3-vl-reranker-8b'  // RAG精排（Qwen3-VL-Reranker-8B）
  }
};
