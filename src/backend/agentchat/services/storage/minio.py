import io
import json
from minio import Minio
from minio.error import S3Error
from loguru import logger
from agentchat.settings import app_settings

class MinioClient:
    def __init__(self):
        self.client = Minio(
            secure=False,
            endpoint=app_settings.storage.minio.endpoint,
            access_key=app_settings.storage.minio.access_key_id,
            secret_key=app_settings.storage.minio.access_key_secret,
        )
        self.bucket_name = app_settings.storage.minio.bucket_name
        # 幂等建桶。
        #
        # ⚠️ 原实现是「先查后建」：
        #       if not bucket_exists(): make_bucket()
        #    这在多 worker 下是 TOCTOU 竞态：两个 worker 可能同时判定桶不存在，
        #    随后一个 make_bucket 成功、另一个抛 BucketAlreadyOwnedByYou。
        #    由于本类在 import 期实例化（services/storage/__init__.py），
        #    该异常会让 lifespan 启动失败 → 进程退出 → 容器反复重启、接口 502。
        #    单 worker 时永远不会触发，故该缺陷在多 worker 生产配置下才暴露。
        #    改为直接建桶并容忍「已存在」类错误；其他错误仍然抛出，不掩盖真实故障。
        try:
            self.client.make_bucket(self.bucket_name)
            logger.success(f"Minio Bucket: {self.bucket_name} created success")
        except S3Error as e:
            if e.code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                logger.info(f"Minio Bucket `{self.bucket_name}` already exists, skip create")
            else:
                raise
        self.ensure_public_read_policy()

    def ensure_public_read_policy(self):
        """为存储桶设置匿名只读策略（幂等）。

        部分接口与前端直接使用 `storage.minio.base_url` 拼接**无签名**地址访问对象
        （例如 api/services/user.py 的头像列表），若桶为私有则返回 403，导致头像与
        图标无法显示。阿里云 OSS 侧通常已配置公共读 ACL，MinIO 侧必须显式设置。
        """
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": ["*"]},
                    "Action": ["s3:GetObject"],
                    "Resource": [f"arn:aws:s3:::{self.bucket_name}/*"],
                }
            ],
        }
        try:
            self.client.set_bucket_policy(self.bucket_name, json.dumps(policy))
            logger.info(f"Minio Bucket `{self.bucket_name}` public-read policy ensured")
        except S3Error as e:
            logger.error(f"Failed to set public-read policy for {self.bucket_name}: {e}")


    def upload_file(self, object_name, data):
        try:
            if isinstance(data, (bytes, bytearray)):
                data_stream = io.BytesIO(data)
                length = len(data)
            else:
                data = data.encode("utf-8") if isinstance(data, str) else data
                data_stream = io.BytesIO(data)
                length = len(data)
            import mimetypes
            content_type, _ = mimetypes.guess_type(object_name)
            metadata = {"Content-Type": content_type} if content_type else None
            self.client.put_object(
                self.bucket_name, object_name, data_stream, length, metadata=metadata
            )
            logger.info(f"File uploaded successfully: {object_name}")
        except S3Error as e:
            logger.error(f"Failed to upload file: {e}")

    def upload_local_file(self, object_name, local_file):
        try:
            self.client.fput_object(self.bucket_name, object_name, local_file)
            logger.info(f"Local file uploaded successfully: {object_name}")
        except S3Error as e:
            logger.error(f"Failed to upload file : {e}")

    def delete_bucket(self):
        try:
            self.client.remove_bucket(self.bucket_name)
            logger.info("Bucket deleted successfully")
        except S3Error as e:
            logger.error(f"Failed to delete bucket: {e}")

    def sign_url_for_get(self, object_name, expiration=3600):
        try:
            from datetime import timedelta
            url = self.client.presigned_get_object(
                self.bucket_name, object_name, expires=timedelta(seconds=expiration)
            )
            return url
        except S3Error as e:
            logger.error(f"Failed to generate GET URL for {object_name}: {e}")

    def download_file(self, object_name, local_file):
        try:
            self.client.fget_object(self.bucket_name, object_name, local_file)
            logger.info(f"File {object_name} downloaded successfully to {local_file}")
        except S3Error as e:
            logger.error(f"Failed to download {object_name} to {local_file}: {e}")

    def list_files_in_folder(self, folder_path):
        """
        列出指定文件夹下的所有文件（不递归，假设文件夹下没有子目录）

        Args:
            folder_path (str): 文件夹路径，需以斜杠 / 结尾

        Returns:
            list: 文件信息列表，每个元素是包含文件名、大小、修改时间等信息的字典
        """
        try:
            if folder_path and not folder_path.endswith('/'):
                folder_path += '/'

            objects = self.client.list_objects(
                self.bucket_name, prefix=folder_path, recursive=False
            )
            files_url = [
                obj.object_name for obj in objects
                if not obj.is_dir and not obj.object_name.endswith('/')
            ]
            return files_url
        except S3Error as e:
            logger.error(f"Failed to list files in folder {folder_path}: {e}")
            return []


