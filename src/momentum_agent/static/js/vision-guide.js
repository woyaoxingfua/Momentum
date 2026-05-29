/**
 * 视觉功能集成指南 - Vision Feature Integration Guide
 * 
 * 这个文件展示了如何将图片上传功能集成到现有的 Momentum Task Agent 界面中
 */

// ============================================
// 快速开始：添加到 chat.js
// ============================================

/*
在你的 chat.js 文件中添加以下代码：

import { fileToBase64 } from './utils.js'; // 需要创建 utils.js

// 添加到 initChat 函数中
export function initChat() {
    const chatForm = document.querySelector('#chatForm');
    const chatInput = document.querySelector('#chatInput');
    
    // 添加图片上传按钮
    const imageButton = document.createElement('button');
    imageButton.type = 'button';
    imageButton.className = 'image-upload-btn';
    imageButton.innerHTML = '📷';
    imageButton.title = '上传图片';
    
    // 隐藏的文件输入
    const imageInput = document.createElement('input');
    imageInput.type = 'file';
    imageInput.accept = 'image/*';
    imageInput.style.display = 'none';
    
    // 当前选中的图片
    let selectedImage = null;
    
    imageButton.onclick = () => imageInput.click();
    
    imageInput.onchange = async (e) => {
        const file = e.target.files[0];
        if (file) {
            selectedImage = await fileToBase64(file);
            
            // 显示图片预览
            const preview = document.createElement('div');
            preview.className = 'image-preview';
            preview.innerHTML = `
                <img src="data:image/jpeg;base64,${selectedImage}" alt="Preview" />
                <button type="button" class="remove-image">✕</button>
            `;
            
            // 添加到聊天区域
            chatForm.insertBefore(preview, chatForm.lastElementChild);
            
            // 移除按钮
            preview.querySelector('.remove-image').onclick = () => {
                selectedImage = null;
                preview.remove();
                imageInput.value = '';
            };
        }
    };
    
    // 将按钮添加到输入框旁边
    chatInput.parentNode.insertBefore(imageButton, chatInput.nextSibling);
}

// 修改 sendChat 函数以支持图片
export async function sendChat(message) {
    // ... 现有代码 ...
    
    // 获取当前选中的图片
    const imagePreview = document.querySelector('.image-preview');
    const imageBase64 = imagePreview ? extractBase64(imagePreview) : null;
    
    const payload = {
        message: message
    };
    
    // 如果有图片，添加到 payload
    if (imageBase64) {
        payload.image_base64 = imageBase64;
    }
    
    const response = await fetch('/api/chat', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${getToken()}`
        },
        body: JSON.stringify(payload)
    });
    
    // 处理响应...
}

// ============================================
// utils.js 工具函数
// ============================================

export async function fileToBase64(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
            const base64 = reader.result.split(',')[1];
            resolve(base64);
        };
        reader.onerror = reject;
        reader.readAsDataURL(file);
    });
}

export function extractBase64(previewElement) {
    const img = previewElement.querySelector('img');
    if (img && img.src.startsWith('data:')) {
        return img.src.split(',')[1];
    }
    return null;
}

// ============================================
// CSS 样式（添加到 app.css）
// ============================================

/*
.image-upload-btn {
    background: transparent;
    border: none;
    font-size: 1.5rem;
    cursor: pointer;
    padding: 0.5rem;
    border-radius: 50%;
    transition: background 0.2s;
}

.image-upload-btn:hover {
    background: rgba(0, 0, 0, 0.05);
}

.image-preview {
    position: relative;
    display: inline-block;
    margin: 0.5rem 0;
}

.image-preview img {
    max-width: 200px;
    max-height: 150px;
    border-radius: 8px;
    border: 2px solid #e5e7eb;
}

.image-preview .remove-image {
    position: absolute;
    top: -8px;
    right: -8px;
    background: #ef4444;
    color: white;
    border: none;
    border-radius: 50%;
    width: 24px;
    height: 24px;
    cursor: pointer;
    font-size: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
}
*/

// ============================================
// 使用示例
// ============================================

/*
使用方式：
1. 用户点击 📷 按钮
2. 选择本地图片文件
3. 图片预览显示在输入框下方
4. 用户可以输入说明文字（如"识别这些待办事项"）
5. 点击发送，图片和文字一起发送到 Agent
6. Agent 分析图片内容并提取任务
*/

// ============================================
// 完整的 React 组件示例（如果使用 React）
// ============================================

/*
import React, { useState } from 'react';
import { fileToBase64 } from './utils';

export default function VisionChatInput({ onSend }) {
    const [message, setMessage] = useState('');
    const [image, setImage] = useState(null);
    const [preview, setPreview] = useState(null);

    const handleImageSelect = async (e) => {
        const file = e.target.files[0];
        if (file) {
            const base64 = await fileToBase64(file);
            setImage(base64);
            setPreview(URL.createObjectURL(file));
        }
    };

    const handleSend = () => {
        onSend(message, image);
        setMessage('');
        setImage(null);
        setPreview(null);
    };

    return (
        <div className="chat-input-container">
            {preview && (
                <div className="image-preview">
                    <img src={preview} alt="Preview" />
                    <button onClick={() => { setImage(null); setPreview(null); }}>
                        ✕
                    </button>
                </div>
            )}
            
            <input
                type="file"
                accept="image/*"
                onChange={handleImageSelect}
                style={{ display: 'none' }}
                ref={input => this.imageInput = input}
            />
            
            <button onClick={() => this.imageInput.click()}>
                📷
            </button>
            
            <input
                type="text"
                value={message}
                onChange={(e) => setMessage(e.target.value)}
                placeholder="发送消息或上传图片..."
            />
            
            <button onClick={handleSend} disabled={!message && !image}>
                发送
            </button>
        </div>
    );
}
*/

console.log('📷 Vision Feature Integration Guide loaded');
console.log('See this file for integration examples and best practices.');
