import base64
from collections.abc import Sequence
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import anyio
from httpx import AsyncClient
import jinja2
from nonebot_plugin_saa import Image, MessageSegmentFactory, Text
from PIL import Image as PILImage
from pydantic import BaseModel
from yarl import URL

from nonebot_bison.compat import model_validator
from nonebot_bison.theme import Theme, ThemeRenderError, ThemeRenderUnsupportError
from nonebot_bison.theme.utils import convert_to_qr, web_embed_image
from nonebot_bison.utils import is_pics_mergable, pic_merge

if TYPE_CHECKING:
    from nonebot_bison.post import Post


async def embed_image_as_data_url(image_path: Path) -> str:
    """读取图片文件并返回base64数据URL字符串

    Args:
        image_path: 图片文件路径

    Returns:
        base64格式的data URL字符串
    """
    if not image_path.exists():
        return ""

    async with await anyio.open_file(image_path, "rb") as f:
        image_data = base64.b64encode(await f.read()).decode()
        return f"data:image/png;base64,{image_data}"

async def embed_svg_as_data_url(svg_path: Path) -> str:
    """读取SVG文件并返回base64数据URL字符串"""
    if not svg_path.exists():
        return ""

    async with await anyio.open_file(svg_path, "r", encoding='utf-8') as f:
        svg_content = await f.read()
        encoded_svg = base64.b64encode(svg_content.encode('utf-8')).decode()
        return f"data:image/svg+xml;base64,{encoded_svg}"


class UserInfo(BaseModel):
    """用户信息部分"""
    name: str
    desc: str | None = None  # 使用 description 字段
    avatar: str | None = None

class Content(BaseModel):
    """内容部分"""
    text: str
    images: list[str] = []
    title: str | None = None  # 使用 title 字段

class Retweet(BaseModel):
    """转发部分"""
    author: str | None = None
    content: str | None = None
    images: list[str] = []
    avatar: str | None = None

class PopinPartyCard(BaseModel):
    """新的卡片数据结构"""
    user: UserInfo
    content: Content
    retweet: Retweet | None = None
    qr_code: str
    timestamp: str
    platform: str


class PopinPartyTheme(Theme):
    """popinparty 分享卡片风格主题

    需要安装`nonebot_plugin_htmlrender`插件
    """

    name: Literal["popinparty"] = "popinparty"
    need_browser: bool = True

    template_path: Path = Path(__file__).parent / "templates"
    parent_path: Path = Path(__file__).parent
    template_name: str = "popinparty.html.jinja"

    async def parse(self, post: "Post") -> tuple[PopinPartyCard, list[str | bytes | Path | BytesIO]]:
        """解析 Post 为 PopinPartyCard 与处理好的图片列表"""
        # 基础验证
        if not post.nickname:
            raise ThemeRenderUnsupportError("post.nickname is None")
        
        # 处理头像
        avatar_url = None
        if post.avatar:
            if isinstance(post.avatar, str):
                avatar_url = post.avatar
            else:
                # 处理非URL头像
                avatar_url = web_embed_image(post.avatar)

        # 创建用户信息 - 充分利用所有可用字段
        user = UserInfo(
            name=post.nickname,
            desc=post.description or "分享美好生活～",  # 使用 description 或默认值
            avatar=avatar_url or "https://via.placeholder.com/100x100/FFB6C1/FFFFFF?text=头像"
        )
        
        http_client = await post.platform.ctx.get_client_for_static()
        images: list[str | bytes | Path | BytesIO] = []
        image_urls: list[str] = []
        
        # 处理主内容图片
        if post.images:
            images = await self.merge_pics(post.images, http_client)
            # 转换为URL用于模板显示
            for img in post.images:
                if isinstance(img, str):
                    image_urls.append(img)
                else:
                    image_urls.append(web_embed_image(img))

        # 创建内容 - 使用 title 和 content
        content_text = ""
        if post.title:
            content_text += f"## {post.title}\n\n"
        content_text += post.content
        
        content = Content(
            text=content_text,
            images=image_urls,
            title=post.title
        )

        # 处理转发 - 充分利用转发 Post 的所有字段
        retweet: Retweet | None = None
        if post.repost:
            retweet_images = []
            retweet_avatar = None
            
            # 处理转发图片
            if post.repost.images:
                repost_images = await self.merge_pics(post.repost.images, http_client)
                images.extend(repost_images)
                for img in post.repost.images:
                    if isinstance(img, str):
                        retweet_images.append(img)
            
            # 处理转发者头像
            if post.repost.avatar:
                if isinstance(post.repost.avatar, str):
                    retweet_avatar = post.repost.avatar
                else:
                    retweet_avatar = web_embed_image(post.repost.avatar)
            
            # 构建转发内容文本
            retweet_content = ""
            if post.repost.title:
                retweet_content += f"## {post.repost.title}\n\n"
            retweet_content += post.repost.content
            
            retweet = Retweet(
                author=post.repost.nickname,
                content=retweet_content,
                images=retweet_images,
                avatar=retweet_avatar
            )

        # 处理时间戳
        if post.timestamp:
            timestamp_str = datetime.fromtimestamp(post.timestamp).strftime("%Y-%m-%d %H:%M:%S")
        else:
            timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 创建卡片
        card = PopinPartyCard(
            user=user,
            content=content,
            retweet=retweet,
            qr_code=web_embed_image(convert_to_qr(post.url or "No URL", back_color=(255, 255, 255))),
            timestamp=timestamp_str,
            platform=post.platform.name
        )

        return card, images

    @staticmethod
    async def merge_pics(
        images: Sequence[str | bytes | Path | BytesIO],
        client: AsyncClient,
    ) -> list[str | bytes | Path | BytesIO]:
        if is_pics_mergable(images):
            pics = await pic_merge(images, client)
        else:
            pics = images
        return list(pics)

    @staticmethod
    def extract_head_pic(pics: list[str | bytes | Path | BytesIO]) -> str:
        head_pic = web_embed_image(pics[0]) if not isinstance(pics[0], str) else pics[0]
        return head_pic

    @staticmethod
    def card_link(head_pic: PILImage.Image, card_body: PILImage.Image) -> PILImage.Image:
        """将头像与卡片合并"""

        def resize_image(img: PILImage.Image, size: tuple[int, int]) -> PILImage.Image:
            return img.resize(size)

        # 统一图片宽度
        head_pic_w, head_pic_h = head_pic.size
        card_body_w, card_body_h = card_body.size

        if head_pic_w > card_body_w:
            head_pic = resize_image(head_pic, (card_body_w, int(head_pic_h * card_body_w / head_pic_w)))
        else:
            card_body = resize_image(card_body, (head_pic_w, int(card_body_h * head_pic_w / card_body_w)))

        # 合并图片
        card = PILImage.new("RGBA", (head_pic.width, head_pic.height + card_body.height))
        card.paste(head_pic, (0, 0))
        card.paste(card_body, (0, head_pic.height))
        return card

    async def render(self, post: "Post") -> list[MessageSegmentFactory]:
        card, merged_images = await self.parse(post)

        from nonebot_plugin_htmlrender import get_new_page

        template_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(self.template_path),
            enable_async=True,
        )
        template = template_env.get_template(self.template_name)

        # 获取邦多利logo
        bang_dream_logo = await embed_svg_as_data_url(self.template_path / "bang-dream-seeklogo.svg")
        
        html = await template.render_async(
            card=card,
            bang_dream_logo=bang_dream_logo
        )
        
        # 根据内容动态调整视口大小
        base_height = 600
        if card.content.images:
            base_height += len(card.content.images) * 50  # 根据图片数量增加高度
        if card.retweet:
            base_height += 150  # 转发内容额外高度
        
        pages = {
            "device_scale_factor": 2,
            "viewport": {"width": 450, "height": min(base_height, 1200)},  # 限制最大高度
            "base_url": self.template_path.as_uri(),
        }
        
        try:
            async with get_new_page(**pages) as page:
                await page.goto("about:blank")
                await page.set_content(html)
                await page.wait_for_timeout(200)  # 确保所有资源加载完成
                screenshot = await page.screenshot(
                    type="jpeg",
                    quality=90,
                    full_page=True
                )
        except Exception as e:
            raise ThemeRenderError(f"Render error: {e}") from e

        msgs: list[MessageSegmentFactory] = [Image(screenshot)]
        return msgs