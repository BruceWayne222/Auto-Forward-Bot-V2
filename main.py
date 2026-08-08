import asyncio
import os
import logging
import time
import aiohttp
from aiohttp import web
from pyrogram import idle
from bot import Bot

# Calculate uptime
START_TIME = time.time()

def get_uptime():
    elapsed = time.time() - START_TIME
    days, rem = divmod(elapsed, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{int(days)}d {int(hours)}h {int(minutes)}m {int(seconds)}s"

async def web_server():
    async def handle(request):
        uptime = get_uptime()
        html_content = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Bot Status</title>
            <style>
                body {{
                    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                    background-color: #f0f2f5;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    height: 100vh;
                    margin: 0;
                }}
                .container {{
                    background-color: white;
                    padding: 40px;
                    border-radius: 12px;
                    box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
                    text-align: center;
                    max-width: 400px;
                    width: 100%;
                }}
                h1 {{
                    color: #1a73e8;
                    margin-bottom: 20px;
                }}
                p {{
                    color: #555;
                    font-size: 18px;
                    margin: 10px 0;
                }}
                .status-active {{
                    color: #28a745;
                    font-weight: bold;
                }}
                .footer {{
                    margin-top: 30px;
                    font-size: 14px;
                    color: #888;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>Bot is Running</h1>
                <p>Status: <span class="status-active">Active</span></p>
                <p>Uptime: {uptime}</p>
                <div class="footer">
                    Powered by Auto Forward Bot V2
                </div>
            </div>
        </body>
        </html>
        """
        return web.Response(text=html_content, content_type='text/html')

    app = web.Application()
    app.add_routes([web.get('/', handle)])
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('PORT', 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"Web server started on port {port}")

async def ping_server():
    while True:
        await asyncio.sleep(300) # Ping every 5 minutes
        try:
            port = int(os.environ.get('PORT', 8080))
            url = f'http://127.0.0.1:{port}'
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    logging.info(f"Self-ping to {url}: Status {resp.status}")
        except Exception as e:
            logging.error(f"Self-ping failed: {e}")

async def main():
    # Bind HTTP port FIRST so Render detects an open port quickly.
    # (If bot.start() runs first, DB/restart messages can delay >30s → "No open ports")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%I:%M:%S %p",
    )
    try:
        await web_server()
    except Exception as e:
        logging.error(f"Web server failed to start: {e}")
        # Continue anyway — bot can still work as a background worker

    asyncio.create_task(ping_server())

    bot = Bot()
    try:
        await bot.start()
    except Exception as e:
        logging.error(f"Bot failed to start: {e}")
        import traceback
        traceback.print_exc()
        # Keep process alive so Render doesn't loop-crash; web health still works
        while True:
            await asyncio.sleep(3600)

    await idle()
    try:
        await bot.stop()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logging.error(f"Fatal main error: {e}")
        raise
