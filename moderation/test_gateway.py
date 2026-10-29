from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from mediafiles.models import MediaAsset
from . import gateway


@override_settings(
    TENCENT_CONTENT_REVIEW_ENABLED=True,
    TENCENT_CONTENT_REVIEW_SECRET_ID="offline-id",
    TENCENT_CONTENT_REVIEW_SECRET_KEY="offline-secret",
    TENCENT_CONTENT_REVIEW_REGION="ap-guangzhou",
    TENCENT_CONTENT_REVIEW_IMAGE_BIZ_TYPE="",
    TENCENT_CONTENT_REVIEW_VIDEO_BIZ_TYPE="test_video",
)
class MediaGatewayContractTests(SimpleTestCase):
    def setUp(self):
        self.asset = MediaAsset(
            scope="public",
            category="provider_photo",
            status="uploaded",
            object_key="public/image.jpg",
        )

    @patch(
        "mediafiles.services.build_media_url", return_value="https://media.example.com/image.jpg"
    )
    def test_image_uses_official_sdk_file_url_and_data_id(self, _url):
        from tencentcloud.ims.v20201229.models import ImageModerationResponse

        response = ImageModerationResponse()
        response.from_json_string(
            '{"Suggestion":"Review","RequestId":"req-1","DataId":"content-1","Label":"Porn","Score":50}'
        )
        with patch(
            "tencentcloud.ims.v20201229.ims_client.ImsClient.ImageModeration", return_value=response
        ) as call:
            result = gateway.detect_image(self.asset, "content-1")
        request = call.call_args.args[0]
        self.assertEqual(request.FileUrl, "https://media.example.com/image.jpg")
        self.assertEqual(request.DataId, "content-1")
        self.assertEqual(result["suggestion"], "Review")

    @patch(
        "mediafiles.services.build_media_url", return_value="https://media.example.com/video.mp4"
    )
    def test_video_creation_uses_official_sdk_task_input_and_requires_accepted_id(self, _url):
        from tencentcloud.vm.v20210922.models import CreateVideoModerationTaskResponse

        response = CreateVideoModerationTaskResponse()
        response.from_json_string(
            '{"RequestId":"req-1","Results":[{"DataId":"content-1","TaskId":"task-1","Code":"200","Message":"success"}]}'
        )
        with patch(
            "tencentcloud.vm.v20210922.vm_client.VmClient.CreateVideoModerationTask",
            return_value=response,
        ) as call:
            result = gateway.create_video(self.asset, "content-1")
        request = call.call_args.args[0]
        self.assertEqual(request.BizType, "test_video")
        self.assertEqual(request.Type, "VIDEO")
        self.assertEqual(request.Tasks[0].Input.Type, "URL")
        self.assertEqual(request.Tasks[0].Input.Url, "https://media.example.com/video.mp4")
        self.assertEqual(result["task_id"], "task-1")
        with patch(
            "moderation.gateway._call",
            return_value={
                "RequestId": "req-1",
                "Results": [{"DataId": "content-1", "Code": "Error"}],
            },
        ):
            with self.assertRaises(gateway.DetectionUnavailable):
                gateway.create_video(self.asset, "content-1")

    def test_query_preserves_video_labels_and_never_downgrades_nested_review(self):
        from tencentcloud.vm.v20210922.models import DescribeTaskDetailResponse

        response = DescribeTaskDetailResponse()
        response.from_json_string(
            '{"RequestId":"req-1","TaskId":"task-1","DataId":"content-1","Status":"FINISH","Suggestion":"Pass","Labels":[{"Label":"Porn","Suggestion":"Review","Score":70}]}'
        )
        with patch(
            "tencentcloud.vm.v20210922.vm_client.VmClient.DescribeTaskDetail", return_value=response
        ) as call:
            result = gateway.query_video("task-1", "content-1")
        self.assertEqual(call.call_args.args[0].TaskId, "task-1")
        self.assertFalse(call.call_args.args[0].ShowAllSegments)
        self.assertEqual(result["suggestion"], "Review")
        self.assertEqual(result["label"], "Porn")
        self.assertEqual(result["score"], 70)

    def test_private_assets_and_http_urls_are_not_sent(self):
        with patch("moderation.gateway._call") as call:
            self.asset.scope = "private"
            with self.assertRaises(gateway.DetectionUnavailable):
                gateway.detect_image(self.asset, "content-1")
            self.asset.scope = "public"
            with patch(
                "mediafiles.services.build_media_url", return_value="http://media.example.com/x.jpg"
            ):
                with self.assertRaises(gateway.DetectionUnavailable):
                    gateway.detect_image(self.asset, "content-1")
        call.assert_not_called()
