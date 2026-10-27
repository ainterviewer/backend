# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "faker>=40.43.0",
# ]
# ///
import csv

from faker import Faker

fake = Faker("en_US")
Faker.seed(42)

TARGET_PER_GENDER = 10000
generated = set()
results = []

for gender, generator in [
    ("male", fake.first_name_male),
    ("female", fake.first_name_female),
]:
    while sum(row["gender"] == gender for row in results) < TARGET_PER_GENDER:
        first_name = generator()
        last_name = fake.last_name()
        full_name = f"{first_name} {last_name}"

        if full_name not in generated:
            generated.add(full_name)
            results.append(
                {
                    "first_name": first_name,
                    "last_name": last_name,
                    "full_name": full_name,
                    "gender": gender,
                }
            )

with open("generated_names.csv", "w", newline="", encoding="utf-8") as file:
    writer = csv.DictWriter(file, fieldnames=results[0].keys())
    writer.writeheader()
    writer.writerows(results)

print(f"Generated {len(results)} unique names.")
