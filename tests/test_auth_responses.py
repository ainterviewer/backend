"""Auth routes return typed bodies and still set their cookies.

They used to build a `JSONResponse` by hand to put cookies on it, which left
them without a response schema. They now take FastAPI's `Response` for the
cookies and return a model, which only works if FastAPI carries those cookies
over -- so this goes through a real request.
"""

from fastapi.testclient import TestClient

from app.main import app


def test_exit_clears_the_interview_cookies_and_says_so():
    response = TestClient(app).post("/api/exit")

    assert response.status_code == 200
    assert response.json() == {"message": "Successfully exited"}
    cleared = response.headers.get_list("set-cookie")
    assert any(c.startswith("interview_token=") for c in cleared)
    assert any(c.startswith("config=") for c in cleared)


def test_login_declares_its_two_factor_answer():
    responses = app.openapi()["paths"]["/api/login"]["post"]["responses"]

    assert responses["202"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/AuthResponse"
    }
