/**
 * 视觉功能使用示例 - Image Upload Demo for Momentum Task Agent
 * 
 * 这个文件展示了如何在前端实现图片上传和任务识别功能
 */

// ============================================
// 方式1: 原生 JavaScript 实现
// ============================================

class VisionTaskExtractor {
    constructor(apiBaseUrl = '/api') {
        this.apiBaseUrl = apiBaseUrl;
    }

    /**
     * 将图片文件转换为 base64
     */
    async fileToBase64(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => {
                // 移除 data:image/...;base64, 前缀，只保留 base64 数据
                const base64 = reader.result.split(',')[1];
                resolve(base64);
            };
            reader.onerror = reject;
            reader.readAsDataURL(file);
        });
    }

    /**
     * 上传图片并提取任务
     */
    async extractTasksFromImage(file, message = '') {
        try {
            const imageBase64 = await this.fileToBase64(file);
            
            const response = await fetch(`${this.apiBaseUrl}/chat`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': `Bearer ${this.getToken()}`
                },
                body: JSON.stringify({
                    message: message || '请分析这张图片，提取其中的任务并创建待办事项',
                    image_base64: imageBase64
                })
            });

            if (!response.ok) {
                throw new Error(`API 请求失败: ${response.status}`);
            }

            const data = await response.json();
            return {
                success: true,
                message: data.message,
                tasks: this.parseTasks(data.message)
            };
        } catch (error) {
            console.error('提取任务失败:', error);
            return {
                success: false,
                error: error.message
            };
        }
    }

    /**
     * 从 AI 回复中解析任务列表
     */
    parseTasks(aiMessage) {
        // 简单的任务解析逻辑
        const tasks = [];
        const taskRegex = /任务\s*#?(\d+)[:：]\s*(.+?)(?=任务\s*#?|$)/gi;
        let match;
        
        while ((match = taskRegex.exec(aiMessage)) !== null) {
            tasks.push({
                id: match[1],
                title: match[2].trim()
            });
        }
        
        return tasks;
    }

    /**
     * 获取认证 token（需要根据实际实现）
     */
    getToken() {
        return localStorage.getItem('momentum_token') || '';
    }
}

// ============================================
// 方式2: React Hook 实现（如果使用 React）
// ============================================

/*
import { useState } from 'react';

function useVisionTaskExtractor() {
    const [uploading, setUploading] = useState(false);
    const [tasks, setTasks] = useState([]);
    const [error, setError] = useState(null);

    const extractTasks = async (imageFile, message) => {
        setUploading(true);
        setError(null);

        try {
            const base64 = await fileToBase64(imageFile);
            
            const response = await fetch('/api/chat', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': `Bearer ${getToken()}`
                },
                body: JSON.stringify({
                    message: message || '请分析这张图片，提取任务',
                    image_base64: base64
                })
            });

            const data = await response.json();
            
            if (data.error) {
                throw new Error(data.error);
            }

            setTasks(data.tasks || []);
            return data;
        } catch (err) {
            setError(err.message);
            throw err;
        } finally {
            setUploading(false);
        }
    };

    return { extractTasks, uploading, tasks, error };
}

// ============================================
// 使用示例
// ============================================

// HTML 使用方式:
// <input type="file" accept="image/*" onchange="handleImageUpload(this.files[0])" />

async function handleImageUpload(file) {
    const extractor = new VisionTaskExtractor();
    
    // 可选：添加用户说明
    const userMessage = '从这张截屏中提取待办事项';
    
    const result = await extractor.extractTasksFromImage(file, userMessage);
    
    if (result.success) {
        console.log('识别到的任务:', result.tasks);
        // 可以更新 UI 显示任务列表
    } else {
        console.error('提取失败:', result.error);
    }
}
*/

// ============================================
// 导出到全局（如果需要）
// ============================================
if (typeof window !== 'undefined') {
    window.VisionTaskExtractor = VisionTaskExtractor;
}
