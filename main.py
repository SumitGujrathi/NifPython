if __name__ == "__main__":
    # DO NOT run app.run(debug=True) in production
    # Instead, use a WSGI server:
    from waitress import serve
    print("Starting Production Server with Waitress on http://0.0.0.0:5000")

    # 'app' is your Flask object
    # Note: Debug is automatically disabled here.
    serve(app, host='0.0.0.0', port=5000)
  
