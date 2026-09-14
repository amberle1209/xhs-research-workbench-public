/** Finite URL-free processing state shared by protocol, controller and popup. */
export const videoStatuses = ["none", "preparing_model", "running", "complete", "skipped_too_long", "skipped_no_audio", "no_speech", "failed", "not_started"] as const;
export const videoReasons = ["video_not_saved", "duration_unknown", "audio_unreadable", "dependencies_unavailable", "model_preparation_failed", "processing_timeout", "task_interrupted", "stopped", "worker_start_failed", "transcript_too_large", "report_update_failed", "job_in_progress"] as const;
export type VideoProcessing = Readonly<{ status: typeof videoStatuses[number]; reason?: typeof videoReasons[number]; report_update_failed?: boolean }>;
export function readVideoProcessing(value: unknown): VideoProcessing {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new TypeError("invalid video status");
  const item = value as Record<string, unknown>;
  if (Object.keys(item).some(key => !["status", "reason", "report_update_failed"].includes(key)) || !videoStatuses.includes(item.status as VideoProcessing["status"])) throw new TypeError("invalid video status");
  if (item.reason !== null && item.reason !== undefined && !videoReasons.includes(item.reason as NonNullable<VideoProcessing["reason"]>)) throw new TypeError("invalid video reason");
  if (item.report_update_failed !== undefined && typeof item.report_update_failed !== "boolean") throw new TypeError("invalid report status");
  return {status:item.status as VideoProcessing["status"], ...(item.reason == null ? {} : {reason:item.reason as NonNullable<VideoProcessing["reason"]>}), ...(item.report_update_failed === undefined ? {} : {report_update_failed:item.report_update_failed as boolean})};
}
export function videoIsRunning(value?: VideoProcessing): boolean { return value?.status === "running" || value?.status === "preparing_model"; }
export function videoProcessingCopy(value: VideoProcessing): string {
  if (value.report_update_failed) return value.status === "complete" ? "文字稿已生成，报告更新失败。文字文件已保留在本地结果包中。" : "转录状态更新失败，已保存的视频和上一版报告仍保留。";
  const reminder = "可关闭笔记页和弹窗，请保持 Chrome 运行、电脑不休眠；稍后刷新或重新打开同一报告。";
  switch (value.status) {
    case "preparing_model": return `正在准备转录模型：首次需下载约 465 MB，视频已保存。${reminder}`;
    case "running": return `视频报告已生成，音频转录中。${reminder}`;
    case "complete": return "音频转录已完成。刷新或重新打开本地报告可查看文字稿。";
    case "skipped_too_long": return "超过首期转录时长上限（15 分钟），未转录。视频保存状态见报告。";
    case "skipped_no_audio": return "无音轨，未转录。已保存的视频仍可打开。";
    case "no_speech": return "未识别到可转录人声。已保存的视频仍保留。";
    case "not_started": return "未转录：视频未保存。详情及原笔记链接见报告。";
    case "failed": {
      const reasons: Partial<Record<NonNullable<VideoProcessing["reason"]>, string>> = {processing_timeout:"处理超过 10 分钟", task_interrupted:"任务中断", stopped:"已停止", duration_unknown:"无法确认时长", audio_unreadable:"音频不可读", dependencies_unavailable:"转录组件未安装", model_preparation_failed:"模型准备失败", worker_start_failed:"后台任务未能启动"};
      return `转录失败：${value.reason === undefined ? "本机处理异常" : reasons[value.reason] ?? "本机处理异常"}。已保存的视频和报告仍保留。`;
    }
    default: return "";
  }
}
