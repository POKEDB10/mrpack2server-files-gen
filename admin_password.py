import bcrypt
password = "&zPK!HxcGnEIHt89"
hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
print(f"Hashed password: {hashed}")


import bcrypt

username = "pokedb"  # Replace with your username
password = "&zPK!HxcGnEIHt89"  # Replace with your password
hashed = "$2b$12$p3y95mm7gWgQ64jfzXjDCub2jCTYFL7luYEu/2F0jDfy1pWD8Ax5i"  # Replace with the hash from users.json
if bcrypt.checkpw(password.encode(), hashed.encode()):
    print("Credentials are valid")
else:
    print("Credentials are invalid")        