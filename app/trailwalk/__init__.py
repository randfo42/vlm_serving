"""trailwalk — 로드뷰를 따라 걸으며 산책로를 식별하는 클라이언트.

VLM 서빙(../docs/)은 이미지 1장에 대한 판정만 한다. 어디로 갈지·어떻게 이어갈지·
언제 멈출지는 전부 이쪽 몫이다. 설계 근거는 app/docs/20-app-design.md.
"""
__all__ = ["geo", "imaging", "prompt", "providers", "runlog", "vlm", "walk"]
