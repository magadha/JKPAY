import requests
import json
import uuid
import hmac
import hashlib
import time
import os
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# 設置日誌，僅輸出到終端（Render.com 會自動收集日誌）
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler()  # 僅輸出到終端
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "https://magadha.weebly.com"}})  # 允許來自 magadha.weebly.com 的跨域請求

# 使用環境變數儲存敏感資訊
JKO_PAY_STORE_ID = os.getenv("JKO_PAY_STORE_ID")
JKO_PAY_API_KEY = os.getenv("JKO_PAY_API_KEY")
JKO_PAY_SECRET_KEY = os.getenv("JKO_PAY_SECRET_KEY")
JKO_PAY_ENTRY_URL = os.getenv("JKO_PAY_ENTRY_URL", "https://uat-onlinepay.jkopay.app/platform/entry")
JKO_PAY_INQUIRY_URL = os.getenv("JKO_PAY_INQUIRY_URL", "https://uat-onlinepay.jkopay.app/platform/inquiry")
JKO_PAY_REFUND_URL = os.getenv("JKO_PAY_REFUND_URL", "https://uat-onlinepay.jkopay.app/platform/refund")
BASE_URL = os.getenv("BASE_URL", "https://jkpay.onrender.com")  # 更新為 Render.com 提供的域名
GOOGLE_SCRIPT_URL = os.getenv("GOOGLE_SCRIPT_URL", "https://script.google.com/macros/s/AKfycbwju-slnDJ9RYSgWctfjQ7Yg0FOU4Ur6YFu5UWLlKVPsDuMQ3niQI--2b1T06fWBe7PDQ/exec")

# 街口支付 UAT 環境的 IP 白名單
JKO_UAT_IPS = [
    "125.227.158.49",
    "220.133.77.56",
    "59.124.107.103",
    "175.99.130.66",
    "175.99.130.82"
]

# 檢查必要的環境變數是否存在
required_env_vars = ["JKO_PAY_STORE_ID", "JKO_PAY_API_KEY", "JKO_PAY_SECRET_KEY"]
for var in required_env_vars:
    if not os.getenv(var):
        logger.error(f"缺少必要的環境變數: {var}")
        raise ValueError(f"缺少必要的環境變數: {var}")

# 使用內存儲存訂單（Render.com 文件系統是臨時的）
orders = []

def load_orders():
    return orders

def save_orders(new_orders):
    global orders
    orders = new_orders

# 簽名計算函數（符合街口支付規則）
def generate_signature(payload, secret_key):
    if isinstance(payload, dict):
        # 按文檔中列出的字段順序，而不是字母順序
        ordered_fields = [
            "store_id", "platform_order_id", "currency", "total_price", "final_price",
            "unredeem", "valid_time", "payment_type", "escrow", "products"
        ]
        ordered_payload = {}
        for field in ordered_fields:
            if field in payload:
                ordered_payload[field] = payload[field]
        payload_str = json.dumps(ordered_payload, separators=(',', ':'), ensure_ascii=False)
    else:
        payload_str = payload
    input_bytes = payload_str.encode("utf-8")
    secret_key_bytes = secret_key.encode("utf-8")
    digest = hmac.new(secret_key_bytes, input_bytes, hashlib.sha256).hexdigest()
    return digest

# IP 白名單檢查裝飾器
def check_ip_whitelist(f):
    def wrapper(*args, **kwargs):
        client_ip = request.remote_addr
        logger.info(f"收到來自 IP {client_ip} 的請求")
        if client_ip not in JKO_UAT_IPS:
            logger.error(f"未授權的 IP 地址: {client_ip}")
            return jsonify({"error": "未授權的 IP 地址"}), 403
        return f(*args, **kwargs)
    return wrapper

@app.route("/")
def home():
    logger.info("摩揭陀貓舍街口支付伺服器已啟動！（測試環境）")
    return "摩揭陀貓舍街口支付伺服器已啟動！（測試環境）"

@app.route("/generate_payment", methods=["POST"])
def generate_payment():
    try:
        order_data = request.json
        logger.info(f"收到訂單資料: {order_data}")

        # 根據運送方式動態設置必填字段
        shipping = order_data.get("shipping")
        if shipping == "7-11":
            required_fields = ["totalAmount", "quantity", "name", "email", "phone", "shipping", "payment", "storeInfo", "address"]
        else:
            required_fields = ["totalAmount", "quantity", "name", "email", "phone", "shipping", "payment", "city", "district", "address"]
        missing_fields = [field for field in required_fields if field not in order_data or not order_data[field]]
        if missing_fields:
            logger.error(f"缺少必要的字段: {missing_fields}")
            return jsonify({"error": f"缺少必要的字段: {missing_fields}"}), 400

        total_amount = int(order_data["totalAmount"])
        quantity = int(order_data["quantity"])
        payment_method = order_data["payment"]

        # 輸入驗證
        if total_amount <= 0 or quantity <= 0:
            logger.error("totalAmount 和 quantity 必須為正整數")
            return jsonify({"error": "totalAmount 和 quantity 必須為正整數"}), 400
        if total_amount > 1_000_000:
            logger.error("totalAmount 超過允許的最大值")
            return jsonify({"error": "totalAmount 超過允許的最大值"}), 400

        # 計算每件商品的價格，確保總和等於 total_amount
        base_price = total_amount // quantity
        remainder = total_amount % quantity
        products = []
        for i in range(quantity):
            price = base_price + (1 if i == quantity - 1 else 0) * remainder
            products.append({
                "name": "摩揭陀貓舍 商品",
                "unit_count": 1,
                "unit_price": price,
                "unit_final_price": price,
                "img": "https://example.com/product-image.jpg"  # 可選字段
            })

        if payment_method != "jkopay":
            logger.error(f"不支持的付款方式: {payment_method}")
            return jsonify({"error": f"不支持的付款方式: {payment_method}"}), 400

        # 街口支付邏輯
        platform_order_id = f"ORDER_{uuid.uuid4()}_{int(time.time())}"
        # 設置訂單有效期限（當前時間 + 20 分鐘，UTC+8 時區）
        valid_time = (datetime.now() + timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
        data = {
            "store_id": JKO_PAY_STORE_ID,
            "platform_order_id": platform_order_id,
            "currency": "TWD",
            "total_price": total_amount,
            "final_price": total_amount,
            "unredeem": total_amount,
            "valid_time": valid_time,
            "confirm_url": f"{BASE_URL}/confirm_url",
            "result_url": f"{BASE_URL}/result_url",
            "result_display_url": f"{BASE_URL}/result_display_url",
            "payment_type": "onetime",
            "escrow": False,
            "products": products
        }

        # 計算簽名
        signature = generate_signature(data, JKO_PAY_SECRET_KEY)
        logger.info(f"生成的簽名: {signature}")
        logger.info(f"發送的請求數據: {json.dumps(data, ensure_ascii=False)}")
        logger.info(f"請求頭: {{'Content-Type': 'application/json; charset=utf-8', 'API-KEY': '{JKO_PAY_API_KEY}', 'DIGEST': '{signature}'}}")

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "API-KEY": JKO_PAY_API_KEY,
            "DIGEST": signature
        }

        response = requests.post(JKO_PAY_ENTRY_URL, headers=headers, json=data)
        logger.info(f"發送街口支付請求 - 狀態碼: {response.status_code}, 回應: {response.text}")

        if response.status_code == 200:
            try:
                result = response.json()
            except ValueError as e:
                logger.error(f"無法解析街口支付回應: {response.text}")
                return jsonify({"error": "無法解析街口支付回應"}), 500

            if result.get("result") == "000":
                payment_url = result["result_object"]["payment_url"]
                logger.info(f"街口支付連結生成成功，平台訂單ID: {platform_order_id}, 支付URL: {payment_url}")

                # 保存訂單到內存
                order_to_save = {
                    **order_data,
                    "platform_order_id": platform_order_id,
                    "payment_url": payment_url
                }
                orders = load_orders()
                orders.append(order_to_save)
                save_orders(orders)
                logger.info(f"訂單已保存: {order_to_save}")

                return jsonify({"paymentUrl": payment_url})
            else:
                logger.error(f"街口支付錯誤: {result}")
                return jsonify({"error": f"街口支付錯誤: {result.get('message', '未知錯誤')}"}), 500
        return jsonify({"error": f"無法生成街口支付連結，狀態碼: {response.status_code}"}), 500

    except Exception as e:
        import traceback
        logger.error(f"錯誤: {str(e)}")
        logger.error(f"堆棧跟踪: {traceback.format_exc()}")
        return jsonify({"error": f"伺服器錯誤: {str(e)}"}), 500

@app.route("/confirm_url", methods=["POST"])
@check_ip_whitelist
def confirm_url():
    try:
        logger.info("進入 /confirm_url 路由")
        callback_data = request.json
        logger.info(f"收到街口支付確認回調: {callback_data}")

        platform_order_id = callback_data.get("platform_order_id")
        if not platform_order_id:
            logger.error("無效的確認回調，缺少平台訂單ID")
            return jsonify({"valid": False}), 400

        # 從內存中查找訂單
        orders = load_orders()
        order_data = None
        for order in orders:
            if order["platform_order_id"] == platform_order_id:
                order_data = order
                logger.info(f"找到匹配的訂單: {order_data}")
                break

        if not order_data:
            logger.error(f"找不到對應訂單，平台訂單ID: {platform_order_id}")
            return jsonify({"valid": False}), 404

        # 假設訂單有效（可根據實際需求添加更多驗證邏輯，例如檢查庫存）
        return jsonify({"valid": True})

    except Exception as e:
        import traceback
        logger.error(f"確認錯誤: {str(e)}")
        logger.error(f"堆棧跟踪: {traceback.format_exc()}")
        return jsonify({"valid": False}), 500

@app.route("/result_url", methods=["POST"])
@check_ip_whitelist
def result_url():
    try:
        logger.info("進入 /result_url 路由")
        callback_data = request.json
        logger.info(f"收到街口支付回調: {callback_data}")

        transaction = callback_data.get("transaction", {})
        platform_order_id = transaction.get("platform_order_id")
        status = transaction.get("status")
        trade_no = transaction.get("tradeNo")

        if not platform_order_id:
            logger.error("無效的回調，缺少平台訂單ID")
            return jsonify({"status": "error", "message": "缺少平台訂單ID"}), 400

        # 從內存中查找訂單
        orders = load_orders()
        order_data = None
        for order in orders:
            if order["platform_order_id"] == platform_order_id:
                order_data = order
                logger.info(f"找到匹配的訂單: {order_data}")
                break

        if not order_data:
            logger.error(f"找不到對應訂單，平台訂單ID: {platform_order_id}")
            return jsonify({"status": "error", "message": "訂單未找到"}), 404

        # 根據狀態處理
        if status == 0:  # 交易成功
            order_data["paymentMethod"] = "jkopay"
            order_data["tradeNo"] = trade_no
            logger.info(f"發送訂單到 Google Apps Script: {order_data}")
            try:
                google_response = requests.post(GOOGLE_SCRIPT_URL, data=order_data)
                logger.info(f"Google Apps Script 回應: {google_response.text}")
            except Exception as e:
                logger.error(f"發送訂單到 Google Apps Script 失敗: {str(e)}")

            # 支付成功後，移除已處理的訂單
            orders = load_orders()
            orders[:] = [order for order in orders if order["platform_order_id"] != platform_order_id]
            save_orders(orders)

            return jsonify({"status": "success", "message": "支付確認成功"})
        else:
            logger.error(f"街口支付確認失敗，狀態碼: {status}")
            return jsonify({"status": "error", "message": "支付確認失敗"}), 400

    except Exception as e:
        import traceback
        logger.error(f"確認錯誤: {str(e)}")
        logger.error(f"堆棧跟踪: {traceback.format_exc()}")
        return jsonify({"status": "error", "message": "伺服器錯誤"}), 500

@app.route("/result_display_url", methods=["GET"])
def result_display_url():
    try:
        logger.info("進入 /result_display_url 路由")
        platform_order_id = request.args.get("platform_order_id")
        if not platform_order_id:
            logger.error("無效的返回，缺少平台訂單ID")
            return '''
            <html>
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>無效的返回</title>
                <style>
                    body { font-family: Arial, sans-serif; text-align: center; padding: 50px; background-color: #f9fafb; }
                    h1 { color: #1f2937; font-size: 24px; }
                    p { color: #6b7280; font-size: 16px; }
                    a { color: #10b981; text-decoration: none; font-weight: bold; }
                    a:hover { text-decoration: underline; }
                </style>
            </head>
            <body>
                <h1>無效的返回</h1>
                <p>缺少平台訂單ID，無法處理您的支付請求。</p>
                <p>點擊 <a href="https://magadha.weebly.com">這裡</a> 返回商店。</p>
            </body>
            </html>
            '''

        # 簡單的返回頁面，支付完成後顯示
        return '''
        <html>
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>支付完成</title>
            <style>
                body { font-family: Arial, sans-serif; text-align: center; padding: 50px; background-color: #f9fafb; }
                h1 { color: #1f2937; font-size: 24px; }
                p { color: #6b7280; font-size: 16px; }
                a { color: #10b981; text-decoration: none; font-weight: bold; }
                a:hover { text-decoration: underline; }
            </style>
        </head>
        <body>
            <h1>支付完成！</h1>
            <p>即將在 3 秒後自動返回商店...</p>
            <p>如果未自動跳轉，請點擊 <a href="https://magadha.weebly.com">這裡</a> 返回商店。</p>
            <script>
                setTimeout(function() {
                    window.location.href = "https://magadha.weebly.com";
                }, 3000);
            </script>
        </body>
        </html>
        '''
    except Exception as e:
        import traceback
        logger.error(f"返回錯誤: {str(e)}")
        logger.error(f"堆棧跟踪: {traceback.format_exc()}")
        return '''
        <html>
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>伺服器錯誤</title>
            <style>
                body { font-family: Arial, sans-serif; text-align: center; padding: 50px; background-color: #f9fafb; }
                h1 { color: #1f2937; font-size: 24px; }
                p { color: #6b7280; font-size: 16px; }
                a { color: #10b981; text-decoration: none; font-weight: bold; }
                a:hover { text-decoration: underline; }
            </style>
        </head>
        <body>
            <h1>伺服器錯誤</h1>
            <p>很抱歉，伺服器發生錯誤。請稍後再試，或聯繫客服。</p>
            <p>點擊 <a href="https://magadha.weebly.com">這裡</a> 返回商店。</p>
        </body>
        </html>
        '''

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8001))  # 使用 Render.com 提供的 PORT，默認為 8001
    app.run(host="0.0.0.0", port=port)
