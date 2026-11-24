"""标定专用 HTTP 路由的注册与契约检查（不依赖 httpx/TestClient）。"""
from __future__ import annotations


def test_calibration_routes_registered():
    from app.routers.calibration import router as cal_router
    from app.routers import ROUTERS

    paths = {route.path: route.methods for route in cal_router.routes}
    expected = {
        "/api/calibration/rules": "POST",
        "/api/calibration/batches": "POST",
        "/api/calibration/overlap/confirm": "POST",
        "/api/calibration/cell/{point}": "GET",
        "/api/calibration/trace/{sample_key:path}": "GET",
        "/api/calibration/batches/{batch_key}/verify": "GET",
        "/api/calibration/rules/{rule_a}/relation/{rule_b}": "GET",
    }
    for path, method in expected.items():
        assert path in paths and method in paths[path]
    # 路由模块已挂到应用路由汇总中（ROUTERS 持有的是模块对象）
    from app.routers import calibration, ROUTERS
    assert calibration in ROUTERS
    # OpenAPI 由实际生效路由生成（:path 转换器在 schema 中归一化为普通参数）
    from app.main import app
    openapi_paths = app.openapi()["paths"]
    expected_openapi = {p.replace(":path", "") for p in expected}
    assert all(path in openapi_paths for path in expected_openapi)
